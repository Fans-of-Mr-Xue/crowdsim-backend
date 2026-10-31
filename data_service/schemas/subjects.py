"""Validate subject library records while preserving their simulation attributes."""
import json
import math

FIELDS = ('name', 'type', 'topic', 'status', 'summary', 'updated', 'tags', 'details', 'agentClass', 'agentPortrait')


def finite_json(value, depth=0):
    if depth > 20:
        raise ValueError('主体属性嵌套过深')
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key.startswith('$') or '.' in key:
                raise ValueError('主体属性字段名无效')
            finite_json(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            finite_json(child, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError('主体属性不能包含非有限数值')
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError('主体属性必须为 JSON 数据')


def strings(value, label):
    if not isinstance(value, list) or len(value) > 200 or any(not isinstance(v, str) or len(v) > 2000 for v in value):
        raise ValueError(f'{label}必须为字符串数组，最多 200 项')


def validate_subject(payload):
    if not isinstance(payload, dict) or set(payload) - set(FIELDS):
        raise ValueError('主体配置含有不支持的字段')
    finite_json(payload)
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 512 * 1024:
        raise ValueError('单个主体配置不能超过 512 KiB')
    data = {key: payload.get(key, '') for key in FIELDS[:6]}
    for key, value in data.items():
        if not isinstance(value, str) or len(value) > (10000 if key == 'summary' else 200):
            raise ValueError(f'{key}必须为有效文本')
    data['name'] = data['name'].strip()
    if not data['name']:
        raise ValueError('主体名称不能为空')
    data['tags'] = payload.get('tags', [])
    strings(data['tags'], '标签')
    data['details'] = payload.get('details', [])
    if not isinstance(data['details'], list) or len(data['details']) > 200 or any(not isinstance(row, list) or len(row) != 2 or any(not isinstance(v, str) for v in row) for row in data['details']):
        raise ValueError('结构化属性必须为名称、值组成的文本对')
    for key in ('agentClass', 'agentPortrait'):
        data[key] = payload.get(key, {})
        if not isinstance(data[key], dict):
            raise ValueError(f'{key}必须为对象')
    agent = data['agentClass']
    if agent:
        if not isinstance(agent.get('classId'), str) or not agent['classId'].strip() or len(agent['classId']) > 200:
            raise ValueError('主体类型标识不能为空且不能超过 200 字符')
        for field in ('capabilities', 'classFields', 'stateFields', 'instanceFields'):
            strings(agent.get(field, []), field)
        for field in ('model', 'name', 'layer', 'color', 'description'):
            if field in agent and not isinstance(agent[field], str):
                raise ValueError(f'主体 {field}必须为文本')
        count = agent.get('defaultCount', 0)
        if type(count) is not int or not 0 <= count <= 1000000:
            raise ValueError('主体默认数量必须为 0 至 1000000 的整数')
        instances = agent.get('instanceExamples', [])
        if not isinstance(instances, list) or len(instances) > 200 or any(not isinstance(item, dict) for item in instances):
            raise ValueError('主体实例必须为对象数组')
        for instance in instances:
            if any(field in instance and not isinstance(instance[field], str) for field in ('objectId', 'name', 'scene', 'state', 'task')):
                raise ValueError('主体实例属性必须为文本')
    portrait = data['agentPortrait']
    if portrait:
        for field in ('role', 'avatar', 'tone', 'sourceNote'):
            if not isinstance(portrait.get(field, ''), str):
                raise ValueError(f'画像 {field}必须为文本')
        if not portrait.get('role', '').strip():
            raise ValueError('角色定位不能为空')
        strings(portrait.get('tags', []), '画像标签')
        facts = portrait.get('facts', [])
        if not isinstance(facts, list) or len(facts) > 200 or any(not isinstance(row, list) or len(row) != 2 or any(not isinstance(v, str) for v in row) for row in facts):
            raise ValueError('画像基础属性格式无效')
        metrics = portrait.get('metrics', [])
        if not isinstance(metrics, list) or len(metrics) > 200 or any(not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str) or type(row[1]) not in (int, float) or not 0 <= row[1] <= 100 or not isinstance(row[2], str) for row in metrics):
            raise ValueError('行为指标必须包含名称、0 至 100 的数值和说明')
        for field in ('personas', 'sources'):
            values = portrait.get(field, [])
            if not isinstance(values, list) or len(values) > 200 or any(not isinstance(item, dict) for item in values):
                raise ValueError(f'画像 {field}必须为对象数组')
        for persona in portrait.get('personas', []):
            strings(persona.get('badges', []), '角色标签')
            if any(not isinstance(persona.get(field, ''), str) for field in ('name', 'meta', 'action')):
                raise ValueError('典型角色属性必须为文本')
        for source in portrait.get('sources', []):
            if any(not isinstance(source.get(field, ''), str) for field in ('title', 'publisher', 'supports', 'url')):
                raise ValueError('画像来源属性必须为文本')
    return data
