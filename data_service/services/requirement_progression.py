"""Attach the next concrete requirement interaction after edits and decisions."""
import asyncio
from copy import deepcopy
import logging
import re
from uuid import uuid4

from pymongo.errors import PyMongoError
from ..schemas.page_assistant import validate_plan
from .requirement_interactions import task_binding, validate_requirement_interactions
from .requirement_dialogue import requirement_dialogue
from .requirement_brief import requirement_brief, projected_context, DEFERRED_NOTE
from ..prediction.consultation_catalog import common_input_rows, model_explanation

logger = logging.getLogger(__name__)


def progression_paused(content):
    return bool(re.search(r'(只|仅).{0,12}(修改|调整|改|时间|名称)|不要继续|不用继续|暂不继续|先不继续|先别|暂停|到这里|先到这|别推进', content))


def asks_to_continue(content):
    text = re.sub(r'[\s，,。.!！?？；;]', '', content)
    return bool(re.fullmatch(r'(?:嗯|好的|好|可以|行|嗯可以|没问题)?(?:继续|继续吧|下一步(?:干啥|做什么|怎么办|怎么做|呢|该做什么)?|接下来(?:干啥|做什么|怎么办)|接着来|然后呢|然后嘞)(?:吧)?', text))


async def advance_requirements(plan, context, history, task, case_search):
    if context['page'] != 'task-definition' or plan.get('action') or progression_paused(context.get('current_request', '')):
        return plan
    projected = projected_context(context, plan['changes'])
    values = {row['id']: row.get('value') for row in projected['fields']}
    editable = {row['id']: row for row in projected['fields'] if row.get('enabled', True)}
    brief = requirement_brief(projected)
    targets = {row['id'] for row in context.get('input_targets', []) if row.get('enabled', True)}
    if not all(row['complete'] for row in brief['workflow'][:2]):
        # Missing scope used to return silently, even if the reply promised a
        # next step. Give a concrete card for the actual missing prerequisite.
        if not plan['input_requests'] and not plan['confirmations']:
            if not brief['workflow'][0]['complete'] and 'goal' in targets:
                plan['input_requests'].append({'target':'goal','title':'明确研究目标' if not values.get('requirement.analysis_goal') else '确认任务名称','detail':'你希望判断什么问题？可以直接描述希望得到的结论。' if not values.get('requirement.analysis_goal') else '研究目标已记录，请给这项任务确定一个名称。'})
            elif not values.get('task.timeWindow') and 'scope' in targets:
                plan['input_requests'].append({'target':'scope','widget':'time-window','title':'补充研究时间窗','detail':'请选择完整的开始和结束日期、时间；跨年时请核对年份。'})
            elif 'scope' in targets:
                plan['input_requests'].append({'target':'scope','title':'确认研究区域','detail':'请选择使用环境库已有区域，或在右侧地图自行圈定范围；仅记录地名还不等于确认边界。'})
        return plan
    # Output selection is already a user decision, not a model-selection side
    # effect. Immediately project its definitions, including old drafts that
    # only stored the checkbox values. Never overwrite user-written indicators.
    research = values.get('requirement.plan') or {}
    if values.get('requirement.expected_outputs') and not research.get('indicators') and 'requirement.plan' in editable:
        research = with_output_definitions(research, values)
        if research['indicators']:
            plan['changes']['requirement.plan'] = research
            editable['requirement.plan']['value'] = research
            values['requirement.plan'] = research
    # These suggestions apply to the crowd scenario; other domains use their
    # own model-guided consultation rather than a crowd-specific fallback.
    topic = ' '.join(str(values.get(key) or '') for key in ('task.title', 'requirement.analysis_goal', 'task.location'))
    if not re.search(r'人群|聚集|拥挤|客流|人流|拥堵|踩踏|散场', topic):
        return plan

    decisions = [m.get('decision') for m in history if m.get('decision')]
    if context.get('user_decision'):
        decisions.append(context['user_decision'])
    answered = {(d.get('message_id'), d.get('confirmation_id')) for d in decisions}
    pending = []
    pending_cases = []
    for message in history:
        if (message.get('execution') or {}).get('status') in ('skipped', 'failed', 'partial'):
            continue
        for card in message.get('plan', {}).get('confirmations', []):
            if card.get('task_binding') is not None and card['task_binding'] != task_binding(context):
                continue
            if card.get('kind') == 'case' and (message.get('id'), card.get('id')) not in answered:
                if card.get('subject_id') not in (values.get('task.references') or []):
                    pending_cases.append(card)
            if card.get('subject_id') == 'progress-methods' and 'Weidmann' in str(card.get('changes', {})):
                continue  # Superseded catalog proposal: explain the new choice before applying it.
            if card.get('kind') != 'proposal' or (message.get('id'), card.get('id')) in answered:
                continue
            if card.get('task_binding') == task_binding(context) and all(values.get(k) == v for k, v in card.get('base_values', {}).items()):
                pending.append(card)

    # Search on reaching the scope milestone, even when the last missing value
    # came from a calendar or environment button (neither needs an LLM).
    offered = {c.get('subject_id') for m in history if (m.get('execution') or {}).get('status') not in ('skipped', 'failed', 'partial')
               for c in m.get('plan', {}).get('confirmations', []) if c.get('kind') == 'case'
               and c.get('task_binding', task_binding(context)) == task_binding(context)}
    offered.update(values.get('task.references') or [])
    offered.update(d.get('subject_id') for d in decisions if d.get('kind') == 'case')
    current_cases = [c for c in plan['confirmations'] if c['kind'] == 'case']
    offered.update(c['subject_id'] for c in current_cases)
    seen_search = any('retrieval' in m.get('plan', {}) for m in history)
    retrieval = plan.get('retrieval') or context.get('retrieved_cases')
    if retrieval is None:
        retrieval = next((m['plan']['retrieval'] for m in reversed(history) if 'retrieval' in m.get('plan', {})), None)
    if len(offered) < 2 and (not seen_search or retrieval is not None):
        try:
            if retrieval is None:
                retrieval = await asyncio.to_thread(case_search.search, topic[:12000], task)
            plan['retrieval'] = retrieval
            candidates = [row for row in retrieval.get('items', []) if row['id'] not in offered]
            for row in candidates[:max(0, 2 - len(offered))]:
                plan['confirmations'].append({'id': uuid4().hex, 'kind': 'case', 'subject_id': row['id'],
                    'title': row['title'], 'detail': row['adoption_note'][:1200], 'task_binding': task_binding(context)})
            if candidates:
                plan['reply'] += '\n\n请分别核对相关案例中可借鉴的内容；历史人数和参数不会直接套用。'
            if len(retrieval.get('items', [])) == 1 and not seen_search:
                plan['reply'] += '\n\n目前仅检索到 1 个相关案例，暂不补入关联不足的记录。'
            elif not retrieval.get('items') and not seen_search:
                plan['reply'] += '\n\n案例库暂未找到相关记录，可以先确定预期输出。'
        except PyMongoError:
            logger.warning('Requirement progression case lookup unavailable')
            plan['reply'] += '\n\n案例库暂时无法读取，先继续明确预期输出。'

    if any(c['kind'] == 'case' for c in plan['confirmations']):
        return plan
    if pending_cases:
        plan['_next_requirement'] = f'请先确认上方“{pending_cases[0]["title"]}”是否值得借鉴；也可以选择不同意，再继续模型和输入。'
        plan['reply'] += '\n\n先确认上方案例的借鉴范围。'
        return plan

    # Completed input rows should not keep an obsolete question alive.
    research = values.get('requirement.plan') or {}
    completed = {row['name'] for row in research.get('data_needs', []) if row.get('status') in ('available', 'assumed') or DEFERRED_NOTE in row.get('detail', '')}
    plan['input_requests'] = [row for row in plan['input_requests'] if row.get('input_name') not in completed]
    # An existing concrete prompt already moves the discussion forward.
    if plan['input_requests'] or any(c['kind'] != 'case' for c in plan['confirmations']):
        return plan
    if pending:
        plan['_next_requirement'] = f'请确认上方的“{pending[-1]["title"]}”，也可以直接说明要改哪里。'
        return plan

    targets = {row['id'] for row in context.get('input_targets', []) if row.get('enabled', True)}
    research = values.get('requirement.plan') or {}
    outputs = values.get('requirement.expected_outputs') or []
    raw = {'reply': '下一步', 'changes': {}}
    if not outputs and 'outputs' in targets:
        field = editable.get('requirement.expected_outputs')
        allowed = (field or {}).get('schema', {}).get('items', {}).get('enum', [])
        suggested = [value for value in ('density', 'bottlenecks', 'comparison') if value in allowed]
        rejected = any(d.get('subject_id') == 'progress-outputs' and d.get('choice') == 'reject' for d in decisions)
        if suggested and not rejected:
            labels = {'density': '人数与密度变化', 'bottlenecks': '拥挤位置与时段', 'comparison': '不同模型结果对比'}
            raw['confirmations'] = [{'kind': 'proposal', 'subject_id': 'progress-outputs', 'title': '确定预期输出',
                'detail': '建议关注：' + '、'.join(labels[x] for x in suggested) + '。是否采用？也可在对话中说明增减。',
                'changes': {'requirement.expected_outputs': suggested, 'requirement.plan': with_output_definitions(research, {**values, 'requirement.expected_outputs': suggested})}}]
        else:
            raw['input_requests'] = [{'target': 'outputs', 'title': '选择预期输出', 'detail': '人数变化、密度热点、拥挤时段和不同模型结果对比，你希望保留哪些？'}]
    elif (not research.get('experiment', {}).get('model_ids') or not research.get('steps')) and 'approach' in targets:
        methods = requirement_dialogue(projected, history, task)['available_numerical_methods']
        names = [row['name'] for row in methods if row.get('recommended')]
        rejected = any(d.get('subject_id') == 'progress-methods' and d.get('choice') == 'reject' for d in decisions)
        if 'requirement.plan' in editable and len(names) == 3 and not rejected:
            raw['confirmations'] = [{'kind': 'proposal', 'subject_id': 'progress-methods', 'title': '确认比较模型',
                'detail': '采用正文介绍的三种模型进行同输入比较；同步更新通用输入和模型选择。参数、重复次数与运行在领域模型之后的独立实验控制页面配置。',
                'changes': {'requirement.plan': suggested_comparison(research, values, names, ((context.get('state') or {}).get('task-definition') or {}))}}]
        else:
            raw['input_requests'] = [{'target': 'approach', 'title': '调整模型比较思路',
                'detail': '你希望调整哪些模型或比较重点？可从已接入的' + '、'.join(row['name'] for row in methods) + '中讨论适用性。'}]
    elif 'inputs' in targets:
        needs = research.get('data_needs') or []
        if not needs and 'requirement.plan' in editable:
            research = deepcopy(research)
            research['data_needs'] = common_input_rows(values, ((context.get('state') or {}).get('task-definition') or {}))
            plan['changes']['requirement.plan'] = research
            editable['requirement.plan']['value'] = research
            needs = research['data_needs']
        missing = next((row for row in needs if row.get('status') in ('pending', 'retrieved') and DEFERRED_NOTE not in row.get('detail', '')), None)
        if missing:
            if missing.get('status') == 'retrieved' and missing.get('source') not in (None, 'unknown') and missing.get('detail', '').strip() and 'requirement.plan' in editable:
                accepted = deepcopy(research)
                next(row for row in accepted['data_needs'] if row['name'] == missing['name'])['status'] = 'available'
                raw['confirmations'] = [{'kind':'proposal','subject_id':'review-input-' + missing['name'],
                    'title':'核对' + missing['name'], 'detail':missing['detail'][:850] + '\n这项资料是否适用于本次研究？同意后仅确认这一项；不同意可说明需要修正之处。',
                    'changes':{'requirement.plan':accepted}}]
                rejected = any(d.get('subject_id') == 'review-input-' + missing['name'] and d.get('choice') == 'reject' for d in decisions)
                if rejected:
                    raw.pop('confirmations')
                    raw['input_requests'] = [{'target':'inputs','input_name':missing['name'],'title':'修正' + missing['name'], 'detail':'请说明已有资料哪里不适用，或提供替代资料及来源；暂时无法核对也可以说明。'}]
            else:
                raw['input_requests'] = [{'target':'inputs','input_name':missing['name'],'title':'补充' + missing['name'],
                    'detail':missing.get('detail','')[:750] + '\n请提供这项资料或来源；暂时没有可点击“暂时没有资料”，保留缺口并继续下一项。'}]
        elif not needs:
            raw['input_requests'] = [{'target':'inputs','title':'补充初始人数资料','detail':'研究开始时区域内大约有多少人？请提供人数或来源；暂时没有可直接说明。'}]
        # Fully reviewed inputs end the questioning loop. The final footer asks
        # for whole-task confirmation; it does not create another generic card.
    if raw.get('confirmations') or raw.get('input_requests'):
        # Validate/bind only the added controls against the post-edit draft.
        # Never authorize extra edits or actions along with a user's button.
        projected = projected_context(projected, plan['changes'])
        projected['user_decision'] = None
        extra = validate_requirement_interactions(validate_plan(raw, projected), projected, task, '')
        for card in extra['confirmations']:
            card['id'] = uuid4().hex
        plan['confirmations'].extend(extra['confirmations'])
        plan['input_requests'].extend(extra['input_requests'])
    return plan


def with_output_definitions(research, values):
    result = {'objective': values['requirement.analysis_goal'], 'indicators': [], 'data_needs': [],
              'steps': [], 'assumptions': [], 'open_questions': [], **deepcopy(research)}
    result['objective'] = values['requirement.analysis_goal']
    if not result['indicators']:
        outputs = values.get('requirement.expected_outputs') or []
        specs = {
            'density': ('人数与密度变化', '人、人/㎡', '各时刻人数、密度及峰值', '初始人数、分时到达与离场、路段有效面积'),
            'bottlenecks': ('拥挤位置与时段', '路段、时刻', '识别拥挤路段和发生时段；阈值依据待确认', '路网、容量、人数变化、密度阈值'),
            'exits': ('模拟出入口排队', '人、分钟', '比较模拟出入口排队人数及持续时间；编号须对应已有路网', '入口分配、通道容量、分时到达与离场'),
            'comparison': ('不同模型结果差异', '随指标确定', '同一输入下比较不同模型结果，不等同精度评估', '共同输入、候选模型；精度评价另需观测数据'),
        }
        result['indicators'] = [dict(name=specs[key][0], unit=specs[key][1], definition=specs[key][2],
            input=specs[key][3], method='对齐空间和时间口径，比较所选数值模型的结果') for key in outputs if key in specs]
    return result


def suggested_comparison(research, values, names, state=None):
    result = with_output_definitions(research, values)
    if not result['data_needs']:
        result['data_needs'] = common_input_rows(values, state)
    from ..prediction.consultation_catalog import MODEL_DETAILS
    result['experiment'] = {'settings': {},
                            'notes': '',
                            **result.get('experiment', {}), 'model_ids': list(MODEL_DETAILS)}
    result['steps'] = [{'name': name, 'method': 'professional',
        'inputs': '共用场地边界、可行走面及障碍物、初始人数、到达与驻留、出入口及目标；模型参数见实验控制台',
        'output': '按已确认的输出指标比较人数、密度或排队结果及不同模型之间的差异'} for name in names]
    return result


def explain_model_proposals(plan, context=None):
    """Model education belongs in the message body, regardless of LLM/fallback path."""
    from ..prediction.catalog import method_catalog
    def selected(research):
        ids = set(research.get('experiment', {}).get('model_ids', []))
        for row in method_catalog():
            if any(row['name'] == step.get('name') for step in research.get('steps', [])):
                ids.add(row['id'])
        return ids
    current = next((row.get('value') for row in (context or {}).get('fields', []) if row['id'] == 'requirement.plan'), {}) or {}
    chosen = set()
    edits = [*plan.get('confirmations', []), {'changes': plan.get('changes', {})}]
    for card in edits:
        research = card.get('changes', {}).get('requirement.plan', {})
        previous = (card.get('base_values') or {}).get('requirement.plan', current) or {}
        ids = selected(research)
        if ids and ids != selected(previous):
            chosen.update(ids)
    if chosen:
        explanation = model_explanation(chosen)
        if explanation not in plan['reply']:
            plan['reply'] = (plan['reply'] + '\n\n' + explanation)[:12000]
    return plan


def defer_input_skill(content, context):
    """Record a specific card's explicit deferral without inventing data."""
    if context.get('page') != 'task-definition':
        return None
    match = re.fullmatch(r'关于“[^”]+”的补充（([^）]+)）：\s*(?:暂时没有这项资料，请保留待补充，先核对下一项。|暂无资料|暂时没有|没有数据)[。！!\s]*', content.strip())
    if not match:
        return None
    field = next((row for row in context['fields'] if row['id'] == 'requirement.plan' and row.get('enabled', True)), None)
    if not field:
        return None
    research = deepcopy(field.get('value') or {})
    row = next((row for row in research.get('data_needs', []) if row['name'] == match[1]), None)
    if not row or row.get('status') not in ('pending', 'retrieved'):
        return None
    row['status'] = 'pending'
    if DEFERRED_NOTE not in row.get('detail', ''):
        row['detail'] = row.get('detail', '')[:800 - len(DEFERRED_NOTE) - 1] + '\n' + DEFERRED_NOTE
    return validate_plan({'reply': f'“{match[1]}”保留为待补充，不使用默认值冒充已有资料。', 'changes': {'requirement.plan': research}}, context)


def finish_requirement_turn(plan, context, content):
    """After the card budget, always end with one real, actionable next step."""
    if context.get('page') != 'task-definition':
        return plan
    brief = requirement_brief(projected_context(context, plan.get('changes')))
    action = (plan.get('action') or {}).get('id')
    waiting = plan.pop('_next_requirement', None)
    if action == 'task.confirm':
        next_step = '确认执行成功后进入领域模型，继续核对地图、人群与运动规则。'
    elif action == 'task.draw-boundary':
        next_step = '请在打开的地图中圈定并确认研究范围。'
    elif progression_paused(content):
        next_step = '按你的要求暂停推进；恢复后继续核对“' + brief['current_stage'] + '”。'
    elif plan.get('confirmations'):
        titles = '、'.join('“' + card['title'] + '”' for card in plan['confirmations'])
        next_step = '请在下方确认卡中处理' + titles + '；同意后才会应用对应内容，也可以不同意并说明调整。'
        if plan.get('input_requests'):
            next_step += '另请补充“' + plan['input_requests'][0]['title'] + '”。'
    elif plan.get('input_requests'):
        next_step = '请通过下方“' + plan['input_requests'][0]['title'] + '”卡片补充；完成后继续核对下一项。'
    elif waiting:
        next_step = waiting
    elif brief['ready_for_review']:
        counts = brief['input_counts']
        unresolved = [row['name'] for row in brief['deferred_inputs']]
        note = ('仍保留缺口：' + '、'.join(unresolved) + '。') if unresolved else ''
        if counts['assumed']:
            note += f"另有 {counts['assumed']} 项按情景假设记录，尚非实测资料。"
        next_step = '研究安排已基本整理完毕。' + note + '请核对右侧，确认无误后点击“确认需求，进入领域模型”；这不代表数据已全部齐备或已开始计算。'
    else:
        next_step = (brief['priorities'] or ['核对右侧尚未明确的内容'])[0] + '。请在对话中补充或说明需要调整之处。'
    # Replace only a terminal next-step paragraph. Keep substantive explanation.
    body = re.sub(r'\n\n(?:\*\*)?下一步[：:][\s\S]*$', '', plan['reply']).rstrip()
    footer = '\n\n下一步：' + next_step
    plan['reply'] = body[:12000 - len(footer)] + footer
    return plan
