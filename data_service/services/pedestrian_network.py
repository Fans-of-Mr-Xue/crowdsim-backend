"""Evidence-preserving pedestrian assets; geometry alone never joins floor levels."""
import copy
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone

from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import transform

WALKWAYS = {'footway', 'path', 'pedestrian', 'steps', 'living_street'}
BUILDER_VERSION = 'pedestrian-assets-2'


def portable_geometry(geometry):
    return json.loads(json.dumps(mapping(geometry)))


def pedestrian(feature):
    p = feature.get('properties') or {}
    return p.get('access') not in ('no', 'private') and p.get('foot') != 'no' and (p.get('highway') in WALKWAYS or p.get('foot') in ('yes', 'designated'))


def level_key(properties):
    value = properties.get('level')
    if value is None or value == '':
        value = properties.get('layer')
    if value is None or value == '':
        return '-1' if properties.get('tunnel') in ('yes', 'building_passage') else '0'
    text = str(value).strip()
    try:
        number = float(text)
        return str(int(number)) if math.isfinite(number) and number.is_integer() else text
    except ValueError:
        return text


def surface_feature(feature):
    return level_key(feature.get('properties') or {}) in ('0', '0.0')


def pieces(geometry, kinds):
    if geometry.is_empty:
        return []
    if geometry.geom_type in kinds:
        return [geometry]
    return [part for item in getattr(geometry, 'geoms', []) for part in pieces(item, kinds)]


def collection(features=()):
    return {'type': 'FeatureCollection', 'features': list(features)}


def metric_transform(boundary):
    lon, lat = boundary.centroid.coords[0]
    metric = CRS.from_proj4(f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m')
    return Transformer.from_crs('EPSG:4326', metric, always_xy=True).transform


def osm_assets(value, geometry, source):
    boundary = shape(geometry)
    elements = value.get('elements', [])
    nodes = {element['id']: element for element in elements if element.get('type') == 'node'}
    ways = {element['id']: element for element in elements if element.get('type') == 'way'}
    lines, areas, obstacles, barriers, entrances = [], [], [], [], []
    for way in ways.values():
        refs = way.get('nodes', [])
        points = [[nodes[ref]['lon'], nodes[ref]['lat']] for ref in refs if ref in nodes]
        if not refs or len(points) != len(refs):
            points = [[p['lon'], p['lat']] for p in way.get('geometry', [])]
        if len(points) < 2:
            continue
        tags = copy.deepcopy(way.get('tags') or {})
        p = {**tags, 'osm_id': way['id'], 'osm_type': 'way', 'osm_nodes': refs,
             'osm_version': way.get('version'), 'osm_timestamp': way.get('timestamp'),
             'source_url': f"https://www.openstreetmap.org/way/{way['id']}",
             'width_source': 'osm_tag' if tags.get('width') else 'missing',
             'level_source': 'osm_tag' if tags.get('level') is not None else 'layer_tag' if tags.get('layer') is not None else 'unmapped_assumed_surface'}
        p['display_level'] = level_key(p)
        is_ring = len(points) >= 4 and points[0] == points[-1]
        polygon = shape({'type': 'Polygon', 'coordinates': [points]}) if is_ring else None
        if polygon is not None and not polygon.is_valid:
            polygon = None
        if polygon is not None:
            area_p = {**p, 'geometry_source': 'osm_polygon'}
            # Closed footway loops remain lines unless explicitly mapped as an area.
            if tags.get('area:highway') in ('pedestrian', 'footway', 'steps') or (tags.get('area') == 'yes' and tags.get('highway') in WALKWAYS):
                areas.append({'type': 'Feature', 'geometry': portable_geometry(polygon), 'properties': {**area_p, 'asset_kind': 'walkable_area'}})
            elif tags.get('building') or tags.get('building:part') or tags.get('landuse') in ('grass', 'flowerbed') or tags.get('natural') == 'water' or tags.get('leisure') == 'garden':
                kind = 'building' if tags.get('building') or tags.get('building:part') else 'water' if tags.get('natural') == 'water' else 'planting'
                obstacles.append({'type': 'Feature', 'geometry': portable_geometry(polygon), 'properties': {**area_p, 'asset_kind': kind}})
        if tags.get('barrier') in ('fence', 'wall', 'retaining_wall', 'hedge'):
            barriers.append({'type': 'Feature', 'geometry': portable_geometry(LineString(points)), 'properties': {**p, 'asset_kind': 'barrier'}})
        if tags.get('highway') and not (is_ring and tags.get('area') == 'yes'):
            feature = {'type': 'Feature', 'geometry': portable_geometry(LineString(points)), 'properties': p}
            if pedestrian(feature):
                lines.append(feature)
    for node in nodes.values():
        tags = node.get('tags') or {}
        if not (tags.get('entrance') or tags.get('barrier') in ('gate', 'entrance', 'lift_gate', 'bollard')):
            continue
        entrances.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [node['lon'], node['lat']]},
                          'properties': {**tags, 'osm_id': node['id'], 'osm_type': 'node', 'display_level': level_key(tags),
                            'source_url': f"https://www.openstreetmap.org/node/{node['id']}", 'status': 'mapped_not_field_verified',
                            'asset_kind': 'building_entrance' if tags.get('entrance') else 'gate'}})
    result = collection(lines)
    result.update(source=source, builder_version=BUILDER_VERSION, walkable_areas=collection(areas), obstacles=collection(obstacles),
                  barriers=collection(barriers), entrances=collection(entrances), boundary=geometry,
                  temporal={'observed_at': source.get('retrievedAt'), 'valid_from': None, 'valid_to': None,
                            'historical_verified': False, 'note': '公开地图现状快照；不能据此确认2014年或其他历史时点的通道布局。'})
    return clip_network(result, geometry)


def clip_network(network, geometry):
    result = copy.deepcopy(network)
    boundary = shape(geometry)
    output = []
    for feature in result.get('features', []):
        if not pedestrian(feature):
            continue
        for index, line in enumerate(pieces(shape(feature['geometry']).intersection(boundary), {'LineString'})):
            if line.length <= 1e-10:
                continue
            p = {**feature.get('properties', {}), 'clip_part': index}
            output.append({'type': 'Feature', 'geometry': portable_geometry(line), 'properties': p})
    result['features'] = output
    for key, kinds in [('walkable_areas', {'Polygon'}), ('obstacles', {'Polygon'}), ('barriers', {'LineString'}), ('entrances', {'Point'})]:
        selected = []
        for feature in result.get(key, {}).get('features', []):
            for part in pieces(shape(feature['geometry']).intersection(boundary), kinds):
                selected.append({**feature, 'geometry': portable_geometry(part)})
        result[key] = collection(selected)
    result['boundary'] = geometry
    result['builder_version'] = network.get('builder_version', BUILDER_VERSION)
    result['topology'] = topology(result)
    result['quality'] = quality(result)
    return result


def topology(network):
    if network.get('simulation_network'):
        return sumo_topology(network)
    boundary = shape(network['boundary'])
    project = metric_transform(boundary)
    nodes, edges, adjacency = {}, [], {}
    for feature_index, feature in enumerate(network['features']):
        p = feature['properties']; floor = level_key(p)
        coords = feature['geometry']['coordinates']
        previous = None
        for point in coords:
            key = f'{floor}:{point[0]:.8f}:{point[1]:.8f}'
            identifier = hashlib.sha256(key.encode()).hexdigest()[:16]
            nodes.setdefault(identifier, {'id': identifier, 'coordinates': point, 'level': floor})
            adjacency.setdefault(identifier, set())
            if previous and previous != identifier:
                a, b = nodes[previous]['coordinates'], point
                length = transform(project, LineString([a, b])).length
                if length > .02:
                    edges.append({'source': previous, 'target': identifier, 'length_m': round(length, 2), 'feature_index': feature_index, 'level': floor})
                    adjacency[previous].add(identifier); adjacency[identifier].add(previous)
            previous = identifier
    components, remaining = [], set(nodes)
    while remaining:
        queue = [remaining.pop()]; size = 0
        while queue:
            current = queue.pop(); size += 1
            for neighbour in adjacency[current] & remaining:
                remaining.remove(neighbour); queue.append(neighbour)
        components.append(size)
    return {'nodes': list(nodes.values()), 'edges': edges, 'component_sizes': sorted(components, reverse=True),
            'dangling_node_count': sum(len(neighbours) == 1 for neighbours in adjacency.values()),
            'policy': '仅同层共享几何顶点连接；几何交叉不自动连通，未知层间连接不推断。'}


def sumo_topology(network):
    """Lane adjacency from the source, never join by visual line intersections."""
    features = network['features'] + network.get('walkable_areas', {}).get('features', [])
    selected = {f['properties'].get('sumo_lane_id') for f in features}
    lanes = {lane['id']: lane for lane in network['simulation_network']['lanes'] if lane['id'] in selected}
    adjacency = {key: set() for key in lanes}
    connections = []
    for item in network['simulation_network']['connections']:
        a, b = item['from_lane'], item['to_lane']
        if a not in lanes or b not in lanes:
            continue
        connections.append({'source': a, 'target': b, 'direction': item.get('direction')})
        adjacency[a].add(b)
        adjacency[b].add(a)
    remaining, sizes = set(lanes), []
    while remaining:
        queue, size = [remaining.pop()], 0
        while queue:
            current = queue.pop()
            size += 1
            for neighbour in adjacency[current] & remaining:
                remaining.remove(neighbour)
                queue.append(neighbour)
        sizes.append(size)
    return {'kind': 'sumo_lane_connections', 'nodes': list(lanes.values()), 'edges': connections,
            'component_sizes': sorted(sizes, reverse=True),
            'dangling_node_count': sum(len(items) == 1 for items in adjacency.values()),
            'policy': '沿用 SUMO 显式车道/步行区域连接；交叉线不自动连接。连通分量按无向关系统计，原始连接方向另行保留。',
            'scope': '源路网拓扑；边界裁剪后的通行空间由求解器进一步限制。'}


def quality(network):
    features = network['features']; counts = Counter(level_key(feature['properties']) for feature in features)
    missing = sum(not feature['properties'].get('width') for feature in features)
    warnings = ['未绘出的步道、草地、围栏及广场仍可能缺失；地图收录不代表现场核验。',
                '当前数值求解器使用地面二维空间；下沉层和隧道单独显示，不与地面直接拼接。',
                '没有层级标签的要素按地面假设展示；真实标高及建筑通行情况仍需核对。']
    if missing:
        warnings.append(f'{missing}条道路缺少公开宽度数据，计算时使用任务假设宽度，不标作实测。')
    if len(counts) > 1:
        warnings.append('存在多个高程层；未确认的楼梯/坡道连接需现场或图纸核对。')
    simulated = sum(feature['properties'].get('width_source') == 'sumo_simulation_parameter' for feature in features)
    if network.get('simulation_network'):
        warnings = [
            '道路、路口步行区域和连接关系来自 SUMO 仿真路网；不是新测绘数据。',
            '宽度沿用 SUMO 仿真参数（包含路口宽度调整），不表示现场实测宽度。',
            '沿用当前纪念塔热点的禁行边设置；内侧环道和指定下沉通道不进入本版可通行路网。',
        ]
        if missing:
            warnings.append(f'{missing}条通道缺少宽度，计算仍需任务假设。')
    return {'feature_count': len(features), 'levels': dict(counts), 'missing_width_count': missing,
            'simulation_width_count': simulated,
            'surface_feature_count': sum(surface_feature(f) for f in features),
            'walkable_area_count': len(network.get('walkable_areas', {}).get('features', [])),
            'obstacle_count': len(network.get('obstacles', {}).get('features', [])),
            'barrier_count': len(network.get('barriers', {}).get('features', [])),
            'mapped_entrance_count': len(network.get('entrances', {}).get('features', [])),
            'topology_components': len(network.get('topology', {}).get('component_sizes', [])),
            'warnings': warnings, 'confidence': 'sumo_simulation_parameters' if network.get('simulation_network') else 'public_mapping_unverified_widths', 'generated_at': datetime.now(timezone.utc).isoformat()}
