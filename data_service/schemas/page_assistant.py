"""Bounded page context and declarative edits; no executable model output."""
import json
import math
import re

PAGES = {'data-governance', 'task-definition', 'execution-plan', 'experiment-control', 'result-output', 'domain-model'}


def finite_json(value, depth=0):
    # Declarative field IDs contain dots. They are JSON strings, never Mongo
    # update paths; the repository stores each plan as serialized JSON.
    if depth > 30:
        raise ValueError('助手数据嵌套过深')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('助手数据不支持非有限数值')
    if isinstance(value, dict):
        if len(value) > 2000 or any(not isinstance(key, str) for key in value):
            raise ValueError('助手数据包含无效字段名')
        for item in value.values():
            finite_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            finite_json(item, depth + 1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError('助手数据必须为 JSON')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('会话标识无效，请重新打开助手')
    return value


def validate_value(value, schema, label):
    kind = schema.get('type')
    valid = {'string': isinstance(value, str), 'number': type(value) in (int, float),
             'integer': type(value) is int, 'boolean': type(value) is bool,
             'array': isinstance(value, list), 'object': isinstance(value, dict)}
    if kind not in valid or not valid[kind]:
        raise ValueError(f'{label} 的类型必须为 {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{label} 必须使用给定选项')
    if kind in ('number', 'integer'):
        if not math.isfinite(value) or value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            raise ValueError(f'{label} 超出允许范围')
    if kind == 'string' and not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 8000):
        raise ValueError(f'{label} 的文本长度不符合要求')
    if kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', math.inf):
            raise ValueError(f'{label} 的项目数量不符合要求')
        if schema.get('uniqueItems') and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise ValueError(f'{label} 不能重复')
        for item in value:
            if schema.get('items'):
                validate_value(item, schema['items'], label)
    if kind == 'object':
        properties = schema.get('properties', {})
        if set(value) - set(properties) or set(schema.get('required', [])) - set(value):
            raise ValueError(f'{label} 包含未知或缺失的字段')
        for key, item in value.items():
            validate_value(item, properties[key], label + '.' + key)


def validate_context(context):
    if not isinstance(context, dict) or context.get('page') not in PAGES:
        raise ValueError('当前页面未接入统一助手')
    finite_json(context)
    if len(json.dumps(context, ensure_ascii=False).encode()) > 160 * 1024:
        raise ValueError('页面上下文过大，请缩小当前数据范围')
    fields, actions = context.get('fields'), context.get('actions')
    reference = context.get('task_reference')
    if reference is not None:
        if not isinstance(reference, dict) or set(reference) - {'id', 'revision', 'title'}:
            raise ValueError('分析任务引用无效')
        identifier(reference.get('id'))
        if type(reference.get('revision')) is not int or reference['revision'] < 1:
            raise ValueError('请重新读取分析任务版本')
        if not isinstance(reference.get('title', ''), str) or len(reference.get('title', '')) > 200:
            raise ValueError('分析任务名称须为不超过 200 字的文本')
    for rows in (fields, actions):
        if not isinstance(rows, list) or len(rows) > 150 or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in rows):
            raise ValueError('页面操作描述无效')
        if len({item['id'] for item in rows}) != len(rows):
            raise ValueError('页面操作 ID 不能重复')
    return context


def normalize_editor_changes(changes, context):
    """Flatten exact declared field paths, never infer arbitrary writable fields.

    Object-valued fields (especially requirement.plan) remain whole values.
    Unknown paths and ambiguous duplicate representations are still rejected.
    """
    if not isinstance(changes, dict):
        return changes
    fields = {item['id'] for item in context['fields']}
    result = {}

    def visit(path, value):
        if path not in fields and isinstance(value, dict) and value and any(key.startswith(path + '.') for key in fields):
            for key, item in value.items():
                visit(path + '.' + key, item)
            return
        if path in result:
            raise ValueError(f'字段 {path} 重复出现，请只保留一个完整值')
        result[path] = value

    for key, value in changes.items():
        if not isinstance(key, str):
            raise ValueError('修改字段 ID 必须为文本')
        visit(key, value)
    return result


def validate_plan(plan, context):
    if not isinstance(plan, dict) or set(plan) - {'reply', 'changes', 'action', 'confirmations', 'input_requests'}:
        raise ValueError('模型返回的页面操作格式无效')
    reply, changes, action = plan.get('reply'), plan.get('changes', {}), plan.get('action')
    if not isinstance(reply, str) or not reply.strip() or len(reply) > 12000 or not isinstance(changes, dict) or len(changes) > 80:
        raise ValueError('模型回复或修改内容无效')
    fields = {item['id']: item for item in context['fields']}
    changes = normalize_editor_changes(changes, context)
    if 'task.timeWindow' in changes:
        from .task_time import normalize_explicit_time_window
        changes['task.timeWindow'] = normalize_explicit_time_window(changes['task.timeWindow'])
    if len(changes) > 80:
        raise ValueError('一次修改字段过多')
    for key, value in changes.items():
        field = fields.get(key)
        if field is None:
            allowed = ', '.join(item['id'] for item in context['fields'] if item.get('enabled', True))
            raise ValueError(f'不支持的字段 ID：{key}。changes 的键只能是：{allowed}。方案子项须放入 requirement.plan 完整对象，不能作为独立字段')
        if not field.get('enabled', True):
            raise ValueError(f'字段 {key} 当前暂不可编辑，请等待页面加载或保存完成')
        validate_value(value, field['schema'], field.get('label', key))
    if action is not None:
        if not isinstance(action, dict) or set(action) != {'id', 'parameters'}:
            raise ValueError('模型操作格式无效')
        allowed = next((item for item in context['actions'] if item['id'] == action['id']), None)
        if allowed is None or not allowed.get('enabled', True):
            raise ValueError('当前页面不允许执行该操作')
        validate_value(action['parameters'], allowed.get('schema', {'type': 'object'}), allowed.get('label', action['id']))
    finite_json(plan)
    from .assistant_confirmations import confirmations
    reviews = confirmations(plan.get('confirmations'))
    if reviews and context['page'] != 'task-definition':
        raise ValueError('当前页面不支持确认建议')
    from .requirement_interactions import input_requests
    requests = input_requests(plan.get('input_requests'), context)
    return {'reply': reply.strip(), 'changes': changes, 'action': action, 'confirmations': reviews, 'input_requests': requests}


EDITOR_ACTIONS = {
    'task-definition': {'task.save', 'task.confirm', 'task.choose', 'task.draw-boundary'},
    'execution-plan': {'plan.save', 'plan.run', 'plan.result', 'plan.definition', 'plan.choose-task', 'plan.refresh'},
    'experiment-control': {'forecast.save', 'forecast.run', 'forecast.refresh-roads', 'forecast.choose-task'},
    'domain-model': {'domain.save', 'domain.confirm', 'domain.definition', 'domain.plan', 'domain.result', 'domain.choose-task', 'domain.refresh'},
}
TASK_FIELDS = {'task.title', 'task.location', 'task.summary', 'task.timeWindow', 'task.type', 'task.environment',
               'requirement.plan',
               'task.dominant_population', 'task.references', 'task.reference_notes', 'task.section',
               *('requirement.' + key for key in ('analysis_goal', 'key_questions', 'known_conditions', 'expected_outputs')),
               *('task.population.' + group for group in ('crowd', 'age', 'origin', 'gender', 'preference'))}
DOMAIN_FIELDS = {'domain.actor_groups', 'domain.relationships', 'domain.cognitive_models', 'domain.custom_methods', 'domain.references', 'domain.population.dominant_type', 'domain.flood_roads',
                 *('domain.population.' + group for group in ('crowd', 'age', 'origin', 'gender', 'preference'))}
FORECAST_EXTRAS = {'entrance_shares', 'arrival_schedule', 'arrival_interpolation', 'allow_boundary_walkable', 'exit_points', 'exit_labels', 'exit_points_note'}


def validate_editor_operations(plan, context, task):
    """Apply business validators independently of browser-supplied field schemas."""
    from ..prediction.inputs import BOUNDS, normalize_settings, normalize_methods
    from .news import parse_date
    page, changes, action = context['page'], plan['changes'], plan['action']
    if page not in EDITOR_ACTIONS:
        return plan
    fields = TASK_FIELDS if page == 'task-definition' else {'view.mode', *('forecast.' + key for key in set(BOUNDS) | FORECAST_EXTRAS)}
    if page == 'domain-model':
        fields |= DOMAIN_FIELDS
    if page == 'experiment-control':
        fields.add('forecast.methods')
    if set(changes) - fields or (action and action['id'] not in EDITOR_ACTIONS[page]):
        raise ValueError('模型返回了该详情页不支持的修改或操作')
    for key, limit in (('task.title', 200), ('task.location', 200), ('task.summary', 8000), ('task.timeWindow', 200)):
        if key in changes:
            value = changes[key]
            if not isinstance(value, str) or len(value) > limit or (key not in ('task.summary', 'task.location') and not value.strip()):
                raise ValueError(f'{key} 必须为不超过 {limit} 字的有效文本')
    if 'task.timeWindow' in changes:
        parts = re.split(r'\s*(?:至|~|～|—|→)\s*', changes['task.timeWindow'].strip())
        if len(parts) != 2 or any(not re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?', part) for part in parts) or parse_date(parts[1]) <= parse_date(parts[0]):
            raise ValueError('任务时间窗须提供完整起止日期时间，结束须晚于开始')
    for key, value in changes.items():
        if key.startswith('task.population.') or (key.startswith('domain.population.') and key != 'domain.population.dominant_type'):
            if not isinstance(value, list) or len(value) > 200:
                raise ValueError('人群分布最多包含 200 类')
            for row in value:
                if not isinstance(row, dict) or set(row) != {'label', 'value'} or not isinstance(row['label'], str) or not row['label'].strip() or len(row['label']) > 200 or type(row['value']) not in (int, float) or not math.isfinite(row['value']) or not 0 <= row['value'] <= 100:
                    raise ValueError('人群分布须包含有效的类别名称与 0–100 的比例')
            if key.startswith('domain.population.') and value and abs(sum(row['value'] for row in value) - 100) > .001:
                raise ValueError('领域模型中每组人群分布合计须为100%')
    if 'domain.population.dominant_type' in changes and changes['domain.population.dominant_type'] not in ('commuter', 'tourism', 'errand'):
        raise ValueError('请使用有效的主导人群类型')
    for key in ('task.references', 'domain.references'):
        if key in changes:
            validate_value(changes[key], {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 200}}, '参考案例')
    if 'domain.flood_roads' in changes:
        validate_value(changes['domain.flood_roads'], {'type': 'array', 'minItems': 1, 'maxItems': 100, 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 200}}, '承灾道路')
    live_fields = {field['id']: field.get('value') for field in context['fields']}
    if 'task.reference_notes' in changes:
        notes = changes['task.reference_notes']
        validate_value(notes, {'type': 'array', 'items': {
            'type': 'object', 'properties': {
                'id': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                'adoption_note': {'type': 'string', 'minLength': 1, 'maxLength': 1200},
            }, 'required': ['id', 'adoption_note'],
        }}, '案例借鉴内容')
        adopted = set(changes.get('task.references', live_fields.get('task.references') or []))
        if len({row['id'] for row in notes}) != len(notes):
            raise ValueError('同一案例的借鉴内容不能重复')
        if any(row['id'] not in adopted or not row['adoption_note'].strip() for row in notes):
            raise ValueError('借鉴内容必须非空，且仅能修改已采纳或本轮明确采纳的案例')
    if 'requirement.plan' in changes:
        from .research_plan import validate_research_plan
        validate_research_plan(changes['requirement.plan'])
        if page == 'task-definition':
            old = (live_fields.get('requirement.plan') or {}).get('experiment', {})
            new = changes['requirement.plan'].get('experiment', {})
            if new.get('settings', {}) != old.get('settings', {}) or new.get('notes', '') != old.get('notes', ''):
                raise ValueError('需求界定第05项只调整模型选择；保留已有参数和备注，实验参数请在独立实验控制页面配置')
    if any(key.startswith('requirement.') for key in changes) or (action and action['id'] == 'task.confirm'):
        from .task_model import REQUIREMENT_FIELDS, validate_requirements
        # Ordinary requirements and research plans have separate contracts.
        # Do not merge component metadata or plan fields into requirements.
        saved = (task or {}).get('components', {}).get('requirements', {})
        current = {**live_fields, **changes}
        requirements = {name: current.get('requirement.' + name, saved.get(name, [] if name == 'expected_outputs' else ''))
                        for name in REQUIREMENT_FIELDS}
        validate_requirements(requirements)
        if action and action['id'] == 'task.confirm' and not requirements.get('analysis_goal', '').strip():
            raise ValueError('确认需求前请补全分析目标')
        if action and action['id'] == 'task.confirm' and 'requirement.plan' in live_fields:
            from .research_plan import validate_research_plan
            research_plan = {**changes.get('requirement.plan', live_fields['requirement.plan']), 'objective': requirements['analysis_goal']}
            validate_research_plan(research_plan, confirmed=True)
    if {'domain.actor_groups', 'domain.relationships'} & set(changes) or (page == 'domain-model' and action and action['id'] in ('domain.save', 'domain.confirm')):
        from .task_model import validate_agent_model
        saved_agents = (task or {}).get('components', {}).get('agent_model', {})
        validate_agent_model({'groups': changes.get('domain.actor_groups', live_fields.get('domain.actor_groups', saved_agents.get('groups', []))),
                              'relationships': changes.get('domain.relationships', live_fields.get('domain.relationships', saved_agents.get('relationships', [])))})
    if 'domain.cognitive_models' in changes:
        from .task_model import validate_cognitive_models
        groups = changes.get('domain.actor_groups', live_fields.get('domain.actor_groups', (task or {}).get('components', {}).get('agent_model', {}).get('groups', [])))
        validate_cognitive_models(changes['domain.cognitive_models'], {group['id'] for group in groups})
    if 'domain.custom_methods' in changes:
        from ..prediction.custom_formulas import normalize_custom_methods
        normalize_custom_methods({'items': changes['domain.custom_methods']})
    if 'forecast.methods' in changes:
        normalize_methods(changes['forecast.methods'], task)
    forecast_changes = {key[9:]: value for key, value in changes.items() if key.startswith('forecast.') and key != 'forecast.methods'}
    if forecast_changes or (action and action['id'] in ('plan.save', 'plan.run', 'forecast.save', 'forecast.run')) or (action and action['id'] in ('domain.save', 'domain.confirm') and any(key.startswith('forecast.') for key in live_fields)):
        if task is None:
            raise ValueError('请先选择或保存数据库分析任务，再调整预测配置')
        current = {field['id'][9:]: field['value'] for field in context['fields'] if field['id'].startswith('forecast.') and field['id'][9:] in set(BOUNDS) | FORECAST_EXTRAS and 'value' in field}
        normalize_settings(task, {**current, **forecast_changes})
    if action and action['id'] == 'plan.run':
        normalize_methods(action['parameters'].get('methods'), task)
    if changes.get('view.mode') and (len(changes) > 1 or action):
        raise ValueError('请先单独切换展示模式，下一轮再调整新模式下的配置')
    return plan
