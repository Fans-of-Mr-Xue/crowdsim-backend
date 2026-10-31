"""Extensible task definitions; GeoJSON is always stored in WGS84."""
import copy
import re
from bson import BSON
from .environments import finite_json, validate_geometry, coordinate

CORE_COMPONENTS = ('region', 'population', 'references')


def validate_task(payload, *, allow_unbounded_draft=False):
    # Only conversation snapshots opt in; formal tasks and confirmed definitions
    # still require a valid region. A named place never fabricates its boundary.
    fields = {'title', 'summary', 'event_id', 'event_label', 'time_window', 'template_case', 'status', 'region', 'population', 'references', 'components'}
    if not isinstance(payload, dict) or set(payload) - fields:
        raise ValueError('分析任务包含不支持的字段')
    finite_json(payload)
    data = copy.deepcopy(payload)
    for key, limit in (('title', 200), ('summary', 8000), ('event_id', 100), ('event_label', 200), ('time_window', 200), ('template_case', 100)):
        value = data.get(key, '')
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f'{key} 必须是长度不超过 {limit} 的文本')
        data[key] = value.strip()
    if not data['title']:
        raise ValueError('请填写任务名称')
    if data.get('status', 'draft') not in ('draft', 'confirmed'):
        raise ValueError('任务状态必须为 draft 或 confirmed')
    data.setdefault('status', 'draft')
    region = data.get('region')
    if not isinstance(region, dict) or region.get('mode') not in ('environment', 'custom'):
        raise ValueError('请选择环境库区域或绘制自定义边界')
    if type(region.get('zoom', 15)) not in (int, float) or not 3 <= region.get('zoom', 15) <= 20:
        raise ValueError('地图缩放级别必须在 3–20 之间')
    if region.get('center') is not None:
        region['center'] = coordinate(region['center'])
    if not isinstance(region.get('name', ''), str) or len(region.get('name', '')) > 200:
        raise ValueError('区域名称必须为长度不超过 200 的文本')
    if region['mode'] == 'environment':
        if not isinstance(region.get('environment_id'), str) or not region['environment_id']:
            raise ValueError('请提供环境库区域 ID')
        if type(region.get('environment_revision')) is not int or region['environment_revision'] < 1:
            raise ValueError('请重新读取环境库区域版本')
    elif not (allow_unbounded_draft and data['status'] == 'draft' and region.get('geometry') is None):
        region['geometry'] = validate_geometry(region.get('geometry'))
    population = data.setdefault('population', {})
    if not isinstance(population, dict):
        raise ValueError('人群画像必须为对象')
    for group in ('crowd', 'age', 'origin', 'gender', 'preference'):
        rows = population.get(group, [])
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError('人群分布必须为列表，最多包含 200 类')
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('label'), str) or not row['label'].strip() or len(row['label']) > 200:
                raise ValueError('请填写有效的人群类别名称')
            if isinstance(row.get('value'), bool) or not isinstance(row.get('value'), (int, float)) or not 0 <= row['value'] <= 100:
                raise ValueError('人群比例必须为 0–100 的数值')
    references = data.setdefault('references', {'items': []})
    if not isinstance(references, dict) or not isinstance(references.get('items'), list):
        raise ValueError('参考案例必须为 items 数组')
    if any(not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] for item in references['items']):
        raise ValueError('参考案例必须包含 ID')
    components = data.setdefault('components', {})
    if not isinstance(components, dict) or len(components) > 100:
        raise ValueError('任务扩展组件必须为对象，最多 100 项')
    for name, component in components.items():
        validate_component(name, component)
    if data['status'] == 'confirmed' and any(not data[key] for key in ('event_id', 'event_label', 'time_window')):
        raise ValueError('确认任务前请补全事件情景和模拟时间窗')
    if data['status'] == 'confirmed' and 'research_plan' in components:
        from .research_plan import validate_research_plan
        validate_research_plan(components['research_plan'], confirmed=True)
    return data


def validate_component(name, value):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name) or name in CORE_COMPONENTS:
        raise ValueError('扩展组件名称须为英文小写、数字或下划线，不能使用核心组件名')
    if not isinstance(value, dict):
        raise ValueError('扩展组件内容必须为 JSON 对象')
    finite_json(value)
    if name == 'requirements':
        from .task_model import validate_requirements
        validate_requirements(value)
    elif name == 'research_plan':
        from .research_plan import validate_research_plan
        validate_research_plan(value)
    elif name == 'agent_model':
        from .task_model import validate_agent_model
        validate_agent_model(value)
    elif name == 'domain_knowledge':
        from .domain_knowledge import validate_domain_knowledge
        validate_domain_knowledge(value)
    elif name == 'custom_methods':
        from ..prediction.custom_formulas import normalize_custom_methods
        normalize_custom_methods(value)
    elif name == 'road_network_binding':
        if set(value) != {'environment_id', 'environment_revision'} or not isinstance(value['environment_id'], str) or not value['environment_id'] or type(value['environment_revision']) is not int or value['environment_revision'] < 1:
            raise ValueError('路网引用须包含环境库区域ID及有效版本号')
    if len(BSON.encode(value)) > 4 * 1024 * 1024:
        raise ValueError('任务组件超过 4 MiB，请将大型文件存入附件服务并引用其 ID')
