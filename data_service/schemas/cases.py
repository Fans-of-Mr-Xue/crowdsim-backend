"""Case metadata, extensible analytical data and reserved relation identifiers."""
import json
import math
from urllib.parse import urlparse

TEXT_FIELDS = ('name', 'type', 'topic', 'status', 'summary', 'updated', 'eventId', 'eventDate',
               'eventEndDate', 'locationName', 'locationType', 'country', 'city', 'sourceLabel', 'sourceUrl')
FIELDS = (*TEXT_FIELDS, 'tags', 'details', 'coordinates', 'datasetLinks', 'sourceLinks', 'timeline',
          'eventRecord', 'analysisProfile', 'projectProfile', 'extensions', 'linkedPlanIds', 'linkedModelIds')


def json_value(value, depth=0):
    if depth > 30:
        raise ValueError('案例数据嵌套过深')
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key.startswith('$') or '.' in key:
                raise ValueError('案例属性字段名无效')
            json_value(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            json_value(child, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError('案例数值必须为有限数')
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError('案例属性必须为 JSON 数据')


def texts(values, label):
    if not isinstance(values, list) or len(values) > 500 or any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in values):
        raise ValueError(f'{label}必须为有效文本数组')
    return list(dict.fromkeys(values))


def url(value):
    if value and (urlparse(value).scheme not in ('http', 'https') or not urlparse(value).netloc):
        raise ValueError('网页链接必须使用有效 http(s) 地址')


def validate_links(payload):
    if not isinstance(payload, dict) or set(payload) - {'linkedPlanIds', 'linkedModelIds'}:
        raise ValueError('关联接口仅接受 linkedPlanIds 和 linkedModelIds')
    return {field: texts(payload.get(field, []), field) for field in ('linkedPlanIds', 'linkedModelIds')}


def validate_case(payload):
    if not isinstance(payload, dict) or set(payload) - set(FIELDS):
        raise ValueError('案例配置含有不支持的字段')
    json_value(payload)
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
        raise ValueError('单个案例配置不能超过 4 MiB，较大的详情请上传 HTML')
    data = {key: payload.get(key, '') for key in TEXT_FIELDS}
    for key, value in data.items():
        limit = 20000 if key == 'summary' else 4000 if key == 'sourceUrl' else 500
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f'{key}必须为有效文本')
    data['name'] = data['name'].strip()
    if not data['name']:
        raise ValueError('案例名称不能为空')
    url(data['sourceUrl'])
    data['tags'] = texts(payload.get('tags', []), '标签')
    data.update(validate_links({field: payload.get(field, []) for field in ('linkedPlanIds', 'linkedModelIds')}))
    data['details'] = payload.get('details', [])
    if not isinstance(data['details'], list) or len(data['details']) > 500 or any(not isinstance(row, list) or len(row) != 2 or any(not isinstance(v, str) for v in row) for row in data['details']):
        raise ValueError('详细属性必须为名称、值组成的文本对')
    coordinates = payload.get('coordinates', [])
    if not isinstance(coordinates, list) or coordinates and (len(coordinates) != 2 or any(type(v) not in (int, float) for v in coordinates) or not -90 <= coordinates[0] <= 90 or not -180 <= coordinates[1] <= 180):
        raise ValueError('坐标必须为 [纬度, 经度]，也可留空')
    data['coordinates'] = coordinates
    for field in ('eventRecord', 'analysisProfile', 'projectProfile', 'extensions'):
        data[field] = payload.get(field, {})
        if not isinstance(data[field], dict):
            raise ValueError(f'{field}必须为对象')
    for field in ('datasetLinks', 'sourceLinks', 'timeline'):
        values = payload.get(field, [])
        if not isinstance(values, list) or len(values) > 500 or any(not isinstance(v, dict) for v in values):
            raise ValueError(f'{field}必须为对象数组')
        for value in values:
            keys = ('timestamp', 'content') if field == 'timeline' else ('name', 'title', 'meta', 'url')
            if any(not isinstance(value.get(key, ''), str) for key in keys):
                raise ValueError(f'{field}属性必须为文本')
            if field != 'timeline':
                url(value.get('url', ''))
        data[field] = values
    return data
