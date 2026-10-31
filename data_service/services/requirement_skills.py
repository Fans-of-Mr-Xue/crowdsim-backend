"""Small, deterministic interaction skills for the pre-event dialogue."""
import re
from ..prediction.consultation_catalog import PARAMETERS
from ..schemas.task_time import explicit_time_request, time_window_duration
from ..schemas.page_assistant import validate_plan, validate_editor_operations


def experiment_parameter_skill(content, context):
    """Route parameter assignments to the dedicated experiment page without edits.

    Every clause must be either a supported assignment or an explicit instruction
    to preserve the rest/pause. Never execute a substring of a broader request.
    """
    if context['page'] != 'task-definition':
        return None
    field = next((row for row in context['fields'] if row['id'] == 'requirement.plan' and row.get('enabled', True)), None)
    if not field or not (field.get('value') or {}).get('experiment'):
        return None
    units = {'m': ('m', '米'), 's': ('s', '秒'), '分钟': ('分钟', '分', 'min'),
             'm/s': ('m/s', '米/秒'), '人/㎡': ('人/㎡', '人/平方米'),
             '人/(m·s)': ('人/(m·s)',), '次': ('次',), '': ('',)}
    edits, descriptions = {}, []
    prefix = r'(?:(?:请|麻烦|帮我|只|仅|把|将|实验控制台的?|实验配置的?|模型参数的?)\s*)*'
    clauses = re.split(r'[，,。；;\n]|(?:并且|并|同时)', content.strip())
    for clause in clauses:
        clause = clause.strip()
        if not clause or re.fullmatch(r'(?:不要|不用|暂不|先不|别)(?:再)?(?:继续|推进)(?:下一步|其他事项|流程)?[。！!]*', clause):
            continue
        if re.fullmatch(r'(?:其余|其他|其它)[^？?]*(?:保持原样|保持不变|不变|不改|不动)', clause):
            continue
        matched = False
        for key, label, unit, *_ in PARAMETERS:
            name = r'\s*'.join(re.escape(part) for part in label.split())
            if key == 'repetitions':
                name = r'(?:实验)?重复次数'
            match = re.fullmatch(prefix + name + r'\s*(?:改为|调整为|设置为|设为|改成|设成|=|：|:)\s*([+-]?\d+(?:\.\d+)?)\s*(.*?)(?:吧|即可)?', clause, re.I)
            if not match or match[2].strip() not in ('', *units[unit]):
                continue
            value = float(match[1])
            if key in edits:  # Conflicting/repeated instructions need interpretation.
                return None
            edits[key] = int(value) if value.is_integer() else value
            descriptions.append(f'{label}：{edits[key]} {unit}'.strip())
            matched = True
            break
        if not matched:
            return None
    if not edits:
        return None
    return validate_plan({'reply': '需求界定第 05 项现在是“模型选择”。你提到的' + '；'.join(descriptions) + '属于实验参数，请在保存需求、整理领域模型后，到侧边栏“实验控制”页面配置。本轮未修改参数。'}, context)


def time_window_skill(content, context):
    """Opening an editor does not need an LLM or authorize a guessed date."""
    if context['page'] != 'task-definition':
        return None
    submitted = re.fullmatch(r'关于“[^”]*(?:时间窗|研究时段|起止时间)[^”]*”的补充：\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2} 至 \d{4}-\d{2}-\d{2} \d{2}:\d{2})', content.strip())
    value = submitted.group(1) if submitted else explicit_time_request(content)
    if value:
        return validate_editor_operations(validate_plan({'reply': f'时间窗：{value}，共{time_window_duration(value)}。已提交右侧更新。', 'changes': {'task.timeWindow': value}}, context), context, None)
    if re.search(r'不要|不用|暂不|别修改', content):
        return None
    intent = re.search(r'(修改|调整|更改|重设|重新选择|设置|编辑|打开).{0,8}(时间窗|时间范围|研究时间|分析时间|研究时段|起止时间)', content)
    reverse = re.search(r'(时间窗|时间范围|研究时间|分析时间|研究时段|起止时间).{0,8}(修改|调整|更改|重设|编辑)', content)
    # Actual dates, durations and relative changes are handled by the normal
    # editing path, so a request containing a new value is not discarded.
    match = intent or reverse
    if match:
        remainder = content[:match.start()] + content[match.end():]
        remainder = re.sub(r'[，。！？?!\s]|我想要|我需要|我要|我想|帮我|麻烦|现在|一下|重新|可以|能否|这个|我|请|吗|吧|的', '', remainder)
        if remainder:
            return None
    if not match or re.search(r'\d|[一二三四五六七八九十两半]+(?:年|月|日|天|时|点|分)|明天|后天|提前|推迟|延长|缩短|明年|今年|去年', content):
        return None
    if not any(row.get('id') == 'scope' and row.get('enabled', True) for row in context.get('input_targets', [])):
        return None
    return validate_plan({'reply': '请在下方选择新的起止日期和时间，点击“发送补充”后同步右侧时间窗。',
                          'input_requests': [{'target': 'scope', 'widget': 'time-window', 'title': '修改研究时间窗',
                                              'detail': '选择开始和结束时间；跨年时请核对结束日期的年份。'}]}, context)


def grounded_requirement_changes(content, context):
    """Copy explicit first-turn facts; do not infer simulation settings or geometry."""
    fields = {row['id']: row for row in context['fields'] if row.get('enabled', True)}
    result = {}
    topic = re.match(r'^(?:我想|我要|请帮我|帮我)?(?:研究|分析|评估)([^。！？!?，,\n]{3,500})', content.strip())
    if topic:
        text = topic.group(1).strip()
        if 'task.title' in fields and not fields['task.title'].get('value'):
            result['task.title'] = text[:200]
        if 'requirement.analysis_goal' in fields and not fields['requirement.analysis_goal'].get('value'):
            result['requirement.analysis_goal'] = '研究' + text
    # Use only a name actually present in this user message. A repository match
    # alone never authorizes selecting that environment or importing its polygon.
    if 'task.location' in fields:
        from .requirement_environments import aliases
        matches = [name for row in context.get('environment_candidates', []) for name in aliases(row) if name in content]
        if matches:
            name = max(matches, key=len)
            end = content.index(name) + len(name)
            suffix = re.match(r'(附近|周边|区域|一带)', content[end:])
            result['task.location'] = name + (suffix.group(0) if suffix else '')
    return result
