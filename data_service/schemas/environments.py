"""Environment profiles and WGS84 polygon validation shared by imports and APIs."""
import copy
import math
from pyproj import Geod

LEVELS = ('city', 'district', 'park', 'landmark', 'custom')
FIELDS = {'title', 'pageId', 'cityCode', 'locationLabel', 'category', 'color', 'status', 'statusLabel',
          'sandboxSubtitle', 'entrySubtitle', 'description', 'cardSummary', 'cardMetrics', 'reuseCount',
          'coordinates', 'geometry', 'polygon', 'dashboard', 'sources', 'assessmentProfile', 'urbanPulse',
          'regionLevel', 'boundarySource', 'roads', 'roadsStatus', 'roadsMessage'}


def finite_json(value, depth=0):
    if depth > 30:
        raise ValueError('画像字段嵌套过深')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('画像不支持非有限数值')
    if isinstance(value, dict):
        if len(value) > 2000 or any(not isinstance(k, str) or k.startswith('$') or '.' in k for k in value):
            raise ValueError('画像包含无效字段名')
        for part in value.values():
            finite_json(part, depth + 1)
    elif isinstance(value, list):
        for part in value:
            finite_json(part, depth + 1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError('画像必须是 JSON 数据')


def coordinate(value):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError('坐标必须为 [经度, 纬度]')
    lon, lat = value[:2]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (lon, lat)) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise ValueError('WGS84 坐标超出经纬度范围')
    return [float(lon), float(lat)]


def validate_geometry(value):
    if not isinstance(value, dict):
        raise ValueError('请提供 Polygon 或 MultiPolygon 边界')
    if value.get('type') == 'Feature':
        return validate_geometry(value.get('geometry'))
    if value.get('type') == 'FeatureCollection':
        polygons = []
        for feature in value.get('features', []):
            geom = validate_geometry(feature)
            polygons.extend([geom['coordinates']] if geom['type'] == 'Polygon' else geom['coordinates'])
        value = {'type': 'MultiPolygon', 'coordinates': polygons}
    kind = value.get('type')
    if kind not in ('Polygon', 'MultiPolygon'):
        raise ValueError('地点坐标不能替代区域边界，请选择面状区域或手动绘制')
    polygons = [value.get('coordinates')] if kind == 'Polygon' else value.get('coordinates')
    if not isinstance(polygons, list) or not polygons:
        raise ValueError('区域边界不能为空')
    total = 0
    result = []
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise ValueError('区域边界缺少外环')
        rings = []
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 3:
                raise ValueError('边界至少需要三个不同顶点')
            points = [coordinate(p) for p in ring]
            if len({tuple(p) for p in points}) < 3:
                raise ValueError('边界至少需要三个不同顶点')
            if points[0] != points[-1]:
                points.append(points[0][:])
            total += len(points)
            if total > 150000:
                raise ValueError('边界顶点过多，请分区域保存或适当简化')
            rings.append(points)
        result.append(rings)
    return {'type': kind, 'coordinates': result[0] if kind == 'Polygon' else result}


def legacy_geometry(polygon):
    if not polygon:
        return None
    rings = polygon if isinstance(polygon[0][0], list) else [polygon]
    return validate_geometry({'type': 'MultiPolygon', 'coordinates': [[[ [p[1], p[0]] for p in ring ]] for ring in rings]})


def geometry_polygons(geometry):
    return [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']


def leaflet_polygon(geometry):
    rings = [[[lat, lon] for lon, lat in polygon[0]] for polygon in geometry_polygons(geometry)]
    return rings[0] if len(rings) == 1 else rings


def geometry_bounds(geometry):
    points = [p for polygon in geometry_polygons(geometry) for p in polygon[0]]
    return [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]


def area_km2(geometry):
    geod = Geod(ellps='WGS84')
    total = 0
    for polygon in geometry_polygons(geometry):
        for index, ring in enumerate(polygon):
            area, _ = geod.polygon_area_perimeter([p[0] for p in ring], [p[1] for p in ring])
            total += abs(area) * (1 if index == 0 else -1)
    return round(max(0, total) / 1000000, 6)


def validate_roads(value):
    if not isinstance(value, dict) or value.get('type') != 'FeatureCollection' or not isinstance(value.get('features'), list):
        raise ValueError('路网必须是 GeoJSON FeatureCollection')
    if len(value['features']) > 15000:
        raise ValueError('路网过大，请按较小区域导入')
    features = []
    for feature in value['features']:
        geometry = feature.get('geometry', {})
        if geometry.get('type') != 'LineString' or len(geometry.get('coordinates', [])) < 2:
            raise ValueError('路网要素必须为包含至少两个顶点的 LineString')
        properties = feature.get('properties') or {}
        if not isinstance(properties, dict):
            raise ValueError('道路属性必须为对象')
        finite_json(properties)
        features.append({'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': [coordinate(p) for p in geometry['coordinates']]}, 'properties': properties})
    result = {'type': 'FeatureCollection', 'features': features}
    if isinstance(value.get('source'), dict):
        finite_json(value['source'])
        result['source'] = copy.deepcopy(value['source'])
    for key in ('walkable_areas', 'obstacles', 'barriers', 'entrances'):
        if key not in value:
            continue
        assets = value[key]
        if not isinstance(assets, dict) or assets.get('type') != 'FeatureCollection' or not isinstance(assets.get('features'), list) or len(assets['features']) > 15000:
            raise ValueError(f'{key} 必须是有效的设施 FeatureCollection')
        selected = []
        for feature in assets['features']:
            geometry = feature.get('geometry') or {}
            kind = geometry.get('type')
            if kind in ('Polygon', 'MultiPolygon') and key in ('walkable_areas', 'obstacles'):
                geometry = validate_geometry(geometry)
            elif kind == 'Point' and key == 'entrances':
                geometry = {'type': 'Point', 'coordinates': coordinate(geometry.get('coordinates'))}
            elif kind == 'LineString' and key == 'barriers':
                if len(geometry.get('coordinates', [])) < 2:
                    raise ValueError('围栏至少需要两个顶点')
                geometry = {'type': kind, 'coordinates': [coordinate(p) for p in geometry['coordinates']]}
            else:
                raise ValueError(f'{key} 包含不支持的几何类型')
            properties = feature.get('properties') or {}
            if not isinstance(properties, dict):
                raise ValueError('设施属性须为对象')
            finite_json(properties)
            selected.append({'type': 'Feature', 'geometry': geometry, 'properties': copy.deepcopy(properties)})
        result[key] = {'type': 'FeatureCollection', 'features': selected}
    for key in ('quality', 'topology', 'temporal', 'boundary', 'builder_version', 'evidence', 'warnings', 'access_audit', 'simulation_network'):
        if key in value:
            finite_json(value[key])
            result[key] = copy.deepcopy(value[key])
    return result


def validate_profile(payload):
    if not isinstance(payload, dict) or set(payload) - FIELDS:
        raise ValueError('区域画像包含不支持的字段')
    data = copy.deepcopy(payload)
    finite_json(data)
    for key, limit in (('title', 200), ('description', 8000), ('locationLabel', 500), ('cityCode', 80)):
        value = data.get(key, '')
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f'{key} 必须是长度不超过 {limit} 的文本')
    if not data.get('title', '').strip():
        raise ValueError('请填写区域名称')
    data['title'] = data['title'].strip()
    if data.get('regionLevel', 'custom') not in LEVELS:
        raise ValueError('区域层级必须为市、区、公园、地标或自定义')
    if data.get('status', 'collecting') not in ('ready', 'collecting'):
        raise ValueError('画像状态必须为 ready 或 collecting')
    data['geometry'] = validate_geometry(data['geometry']) if data.get('geometry') else legacy_geometry(data.get('polygon'))
    if data['geometry'] is None:
        raise ValueError('请先选择或绘制区域边界')
    data.pop('polygon', None)
    bounds = geometry_bounds(data['geometry'])
    center = data.get('coordinates')
    if center:
        if not isinstance(center, list) or len(center) != 2:
            raise ValueError('区域中心必须为 [纬度, 经度]')
        lon, lat = coordinate([center[1], center[0]])
        data['coordinates'] = [lat, lon]
    else:
        data['coordinates'] = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
    data['areaKm2'] = area_km2(data['geometry'])
    if not isinstance(data.get('dashboard', {}), dict) or not isinstance(data.get('sources', []), list):
        raise ValueError('人口画像必须为对象，数据来源必须为数组')
    for field in ('genderSplit', 'ageDistribution', 'originDistribution', 'crowdTypeDistribution', 'preferenceDistribution'):
        rows = data.get('dashboard', {}).get(field, [])
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError('画像分布必须是长度不超过 500 的数组')
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('label'), str) or len(row['label']) > 200:
                raise ValueError('画像分类名称必须为长度不超过 200 的文本')
            value = row.get('value')
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError('画像比例必须在 0–100 之间')
    if 'roads' in data:
        data['roads'] = validate_roads(data['roads'])
    return data
