"""Export an existing SUMO pedestrian network without changing the simulation."""
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString, Polygon, shape
import sumolib

from .pedestrian_network import collection, clip_network, portable_geometry


def export_sumo_network(net_path, hotspot_path, hotspot_id, boundary):
    net_path, hotspot_path = Path(net_path), Path(hotspot_path)
    hotspot = next(item for item in json.loads(hotspot_path.read_text())['hotspots'] if item['id'] == hotspot_id)
    root = ET.parse(net_path).getroot()
    location = root.find('location')
    if location is None or location.get('projParameter') in (None, '!', '-', '.'):
        raise ValueError('SUMO 路网缺少可转换为 WGS84 的投影信息')
    offset = tuple(map(float, location.get('netOffset').split(',')))
    projection = Transformer.from_crs(location.get('projParameter'), 'EPSG:4326', always_xy=True)

    def lonlat(x, y):
        lon, lat = projection.transform(x - offset[0], y - offset[1])
        return [round(lon, 9), round(lat, 9)]

    # SUMO owns lane permissions. Do not infer walkability from road names.
    net = sumolib.net.readNet(str(net_path), withInternal=True, withPedestrianConnections=True)
    region = shape(boundary)
    excluded = set(hotspot.get('excluded_edges', []))
    roles = {}
    for field, label in [('target_edges', '纪念塔外环'), ('entry_edges', '纪念塔连接通道'),
                         ('park_access_edges', '园内步道'), ('park_entry_edges', '公园接入通道')]:
        for identifier in hotspot.get(field, []):
            roles.setdefault(identifier, []).append(label)
    lanes, candidates, excluded_lanes = {}, {}, []
    for edge in net.getEdges(withInternal=True):
        for lane in edge.getLanes():
            if not lane.allows('pedestrian'):
                continue
            points = [lonlat(x, y) for x, y in lane.getShape()]
            function = edge.getFunction() or 'normal'
            if len(points) < (3 if function == 'walkingarea' else 2):
                continue
            geometry = Polygon(points) if function == 'walkingarea' else LineString(points)
            if geometry.is_empty or not geometry.is_valid or not geometry.intersects(region):
                continue
            properties = {
                'highway': 'footway', 'foot': 'yes', 'level': '0', 'display_level': '0',
                'level_source': 'sumo_hotspot_surface_scenario',
                'sumo_edge_id': edge.getID(), 'sumo_lane_id': lane.getID(),
                'sumo_function': function, 'sumo_length_m': lane.getLength(),
                'width': lane.getWidth(), 'width_source': 'sumo_simulation_parameter',
                'speed_mps': lane.getSpeed(), 'name': ' / '.join(roles.get(edge.getID(), [])) or edge.getID(),
                'source_type': 'sumo', 'scenario_id': hotspot_id,
            }
            feature = {'type': 'Feature', 'geometry': portable_geometry(geometry), 'properties': properties}
            if edge.getID() in excluded:
                excluded_lanes.append(feature)
                continue
            lanes[lane.getID()] = {
                'id': lane.getID(), 'edge_id': edge.getID(), 'function': function,
                'from_node': edge.getFromNode().getID(), 'to_node': edge.getToNode().getID(),
                'width_m': lane.getWidth(), 'length_m': lane.getLength(),
                'shape': points, 'pedestrian': True,
            }
            candidates[lane.getID()] = feature

    # Retain SUMO's explicit lane connections, including internal via lanes.
    connections = []
    for item in root.findall('connection'):
        source = item.get('from') + '_' + item.get('fromLane')
        target = item.get('to') + '_' + item.get('toLane')
        via = item.get('via')
        chain = [source, via, target] if via else [source, target]
        for start, end in zip(chain, chain[1:]):
            if start in lanes and end in lanes:
                connections.append({'from_lane': start, 'to_lane': end, 'direction': item.get('dir', '')})
    # Internal junction geometry is useful only when connected to an included
    # pedestrian road. Never include arbitrary car junctions in the park.
    selected = {identifier for identifier, item in lanes.items() if item['function'] not in ('internal', 'walkingarea')}
    changed = True
    while changed:
        changed = False
        for item in connections:
            a, b = item['from_lane'], item['to_lane']
            if (a in selected) != (b in selected):
                selected.update((a, b))
                changed = True
    features, areas = [], []
    for identifier in sorted(selected):
        feature = candidates[identifier]
        if lanes[identifier]['function'] == 'walkingarea':
            feature['properties']['asset_kind'] = 'sumo_walkingarea'
            areas.append(feature)
        else:
            features.append(feature)
    if not features:
        raise ValueError('所选范围内没有符合当前 SUMO 场景通行规则的道路')
    connections = [item for item in connections if item['from_lane'] in selected and item['to_lane'] in selected]
    native = {
        'hotspot_id': hotspot_id,
        'lanes': [lanes[key] for key in sorted(selected)],
        'connections': connections,
        'excluded_edge_ids': sorted(excluded),
        'excluded_roads': collection(excluded_lanes),
        'access_portals': hotspot.get('access_portals', []),
        'target_edges': hotspot.get('target_edges', []),
        'park_entry_edges': hotspot.get('park_entry_edges', []),
        'policy': '继承 SUMO 行人权限、显式连接及当前热点禁行边；连接方向保存原值，双向步行由求解器解释。',
    }
    network = collection(features)
    network.update(
        source={
            'provider': 'sumo', 'label': '上海人民英雄纪念塔 SUMO 仿真路网',
            'filename': net_path.name, 'scenario_id': hotspot_id, 'scope': 'boundary_clipped',
            'coordinateSystem': 'WGS84', 'projection': dict(location.attrib),
            'network_sha256': hashlib.sha256(net_path.read_bytes()).hexdigest(),
            'scenario_sha256': hashlib.sha256(hotspot_path.read_bytes()).hexdigest(),
            'width_status': 'simulation_parameters_not_field_measurements',
        },
        simulation_network=native, walkable_areas=collection(areas),
        builder_version='sumo-pedestrian-1',
        temporal={'historical_verified': False, 'note': '来自当前 SUMO 仿真文件及热点配置，非历史现场测绘。'},
    )
    return clip_network(network, boundary)
