"""Navigation-only requests: a model can name a section, never a DOM selector."""
import re

INPUT_TARGETS = {'goal', 'scope', 'conditions', 'inputs', 'outputs', 'approach', 'cases'}
TEXT_LIMITS = {'target': 30, 'title': 200, 'detail': 1200, 'input_name': 120}


def available_targets(context):
    return {row['id'] for row in context.get('input_targets', [])
            if isinstance(row, dict) and isinstance(row.get('id'), str)
            and row.get('enabled', True)} & INPUT_TARGETS


def input_request_contract(context):
    """Expose the same field limits used by validation to generation and repair."""
    return {'type': 'array', 'items': {
        'type': 'object', 'required': ['target', 'title', 'detail'], 'additionalProperties': False,
        'properties': {key: {'type': 'string', 'minLength': 1, 'maxLength': maximum,
                             **({'enum': sorted(available_targets(context))} if key == 'target' else {})}
                       for key, maximum in TEXT_LIMITS.items()} | {'widget': {'type': 'string', 'enum': ['time-window']}},
    }}


def input_requests(value, context):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError('input_requests须为补充卡片数组')
    if value and context['page'] != 'task-definition':
        raise ValueError('当前页面不支持需求补充卡片')
    available = available_targets(context)
    seen, normalized = set(), []
    for index, item in enumerate(value):
        path = f'input_requests[{index}]'
        if not isinstance(item, dict):
            raise ValueError(f'{path}须为包含target、title、detail的对象')
        missing = {'target', 'title', 'detail'} - set(item)
        unknown = set(item) - (set(TEXT_LIMITS) | {'widget'})
        if missing or unknown:
            raise ValueError(f'{path}字段不符合约定：缺少{sorted(missing)}，未知{sorted(unknown)}；input_name可省略')
        row = dict(item)
        if 'widget' in row and (row['widget'] != 'time-window' or row.get('target') != 'scope'):
            raise ValueError(f'{path}.widget仅支持scope区域的time-window时间选择器')
        # Empty optional metadata means no specific input row. Never infer a
        # target, invent required text, or coerce non-string values into content.
        name = row.get('input_name')
        if name is None or (isinstance(name, str) and not name.strip()):
            row.pop('input_name', None)
        for key, maximum in TEXT_LIMITS.items():
            if key == 'input_name' and key not in row:
                continue
            if not isinstance(row[key], str):
                raise ValueError(f'{path}.{key}须为字符串，实际类型为{type(row[key]).__name__}')
            row[key] = row[key].strip()
            if not row[key]:
                raise ValueError(f'{path}.{key}不能为空；不需要提问时请省略整张卡片')
            if len(row[key]) > maximum:
                raise ValueError(f'{path}.{key}最多{maximum}字，实际{len(row[key])}字；请精简该字段')
        if row['target'] not in available:
            raise ValueError(f'{path}.target只能选择本页可用区域：{sorted(available)}')
        if 'input_name' in row and row['target'] != 'inputs':
            raise ValueError(f'{path}.input_name仅用于inputs；其他区域请省略该字段')
        if row['target'] == 'scope' and (row.get('widget') == 'time-window' or re.search(r'时间|时段|日期', row['title'])):
            row.update(widget='time-window', title='修改研究时间窗' if re.search(r'修改|调整|更改', row['title']) else '补充研究时间窗',
                       detail='请选择开始和结束日期、时间；跨年时请核对年份。')
        identity = (row['target'], row.get('input_name'), row['title'])
        if identity in seen:
            raise ValueError(f'{path}重复询问同一事项，请删除重复卡片；不同事项须分别展示')
        seen.add(identity)
        normalized.append(row)
    return normalized
