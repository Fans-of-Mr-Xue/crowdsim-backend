"""Infer walkable region connections, rather than surveyed physical gates."""
from collections import defaultdict
from functools import lru_cache
from math import atan2, pi
from statistics import median
import xml.etree.ElementTree as ET

from shapely.geometry import LineString, Point, Polygon

from .class2_flow_layout import ROOT


@lru_cache(maxsize=2)
def _osm_regions(path, stamp):
    nodes, regions = {}, []
    for _, item in ET.iterparse(path, events=('end',)):
        if item.tag == 'node':
            nodes[item.get('id')] = (float(item.get('lon')), float(item.get('lat')))
        elif item.tag == 'way':
            tags = {tag.get('k'): tag.get('v') for tag in item.findall('tag')}
            kind = 'park' if tags.get('leisure') == 'park' else 'monument' if tags.get('historic') == 'monument' else None
            if not kind:
                continue
            refs = [node.get('ref') for node in item.findall('nd')]
            if len(refs) < 4 or refs[0] != refs[-1] or any(ref not in nodes for ref in refs):
                continue
            regions.append({'id': 'osm-way-' + item.get('id'), 'name': tags.get('name', ''),
                            'type': kind, 'coordinates': [nodes[ref] for ref in refs]})
    return regions


def _merge_connections(candidates, region, distance):
    groups = []
    for candidate in candidates:
        touching = [group for group in groups if any(
            Point(candidate['point']).distance(Point(other['point'])) <= distance for other in group)]
        if not touching:
            groups.append([candidate])
        else:
            touching[0].append(candidate)
            for group in touching[1:]:
                touching[0].extend(group)
                groups.remove(group)
    center = region['polygon'].centroid
    groups.sort(key=lambda group: atan2(group[0]['point'][0] - center.x, group[0]['point'][1] - center.y) % (2 * pi))
    result = []
    for number, group in enumerate(groups, 1):
        edge_ids = sorted({edge for item in group for edge in item['edgeIds']})
        # Retain a point on a real road instead of averaging across obstacles.
        point = min(group, key=lambda item: (item['point'][0], item['point'][1]))['point']
        result.append({'id': region['id'] + ':' + ','.join(edge_ids), 'regionType': region['type'],
                       'regionName': region['name'], 'toRegionName': group[0]['toRegionName'],
                       'number': number, 'point': list(point), 'edgeIds': edge_ids,
                       'method': sorted({item['method'] for item in group})})
    return result


def find_region_connections(lanes, node_positions, external_edges, site, monument=None):
    """Pure topology/geometry analysis in metres; no scene-specific IDs or points.

    SUMO junction centres and OSM area outlines do not coincide exactly. A
    width-derived tolerance keeps the surrounding ring in the monument region,
    so a ring bend is not mistaken for an entrance.
    """
    width = median(lane['width'] for lane in lanes) if lanes else 2.
    tolerance = width * 3
    site_polygon = site['polygon']
    core_region = monument['polygon'].buffer(tolerance) if monument else None
    site_candidates, core_candidates, endpoints = [], [], defaultdict(list)
    for lane in lanes:
        line = LineString(lane['path'])
        if line.length <= 1e-6:
            continue
        if monument and lane['from'] in node_positions and lane['to'] in node_positions:
            a, b = (Point(node_positions[lane[key]]) for key in ('from', 'to'))
            if core_region.covers(a) != core_region.covers(b) and site_polygon.buffer(tolerance).covers(a) and site_polygon.buffer(tolerance).covers(b):
                core_candidates.append({'point': line.interpolate(.5, normalized=True).coords[0],
                    'edgeIds': [lane['edgeId']], 'toRegionName': site['name'], 'method': 'region-topology'})
        for first, field, node_id in ((True, 'sourceStart', lane['from']), (False, 'sourceEnd', lane['to'])):
            point = tuple(lane['path'][0 if first else -1])
            cut = ('sourceShapeLength' in lane and
                   (lane.get(field, 0) > 1e-6 if first else lane.get(field, lane['sourceShapeLength']) < lane['sourceShapeLength'] - 1e-6))
            if cut:
                if Point(point).distance(site_polygon.boundary) <= tolerance:
                    site_candidates.append({'point': point, 'edgeIds': [lane['edgeId']],
                        'toRegionName': '外部区域', 'method': 'clipped-road'})
            elif node_id in node_positions:
                endpoints[node_id].append({'point': point, 'edgeId': lane['edgeId']})
    links = defaultdict(list)
    native_ids = {lane['edgeId'] for lane in lanes}
    for edge in external_edges:
        if edge['edgeId'] in native_ids or not edge.get('walkable', True):
            continue
        if not any(LineString(path).difference(site_polygon.buffer(width / 8)).length > width
                   for path in edge['paths'] if len(path) > 1):
            continue
        for node_id in (edge['from'], edge['to']):
            links[node_id].append(edge['edgeId'])
    for node_id, records in endpoints.items():
        if not links[node_id]:
            continue
        closest = min(records, key=lambda item: Point(item['point']).distance(site_polygon.boundary))
        if Point(closest['point']).distance(site_polygon.boundary) > tolerance * 2:
            continue
        site_candidates.append({'point': closest['point'],
            'edgeIds': sorted({record['edgeId'] for record in records} | set(links[node_id])),
            'toRegionName': '外部区域', 'method': 'external-topology'})
    portals = _merge_connections(site_candidates, site, width * 2)
    if monument:
        portals = _merge_connections(core_candidates, monument, width * 2) + portals
    return portals, tolerance


def scene_region_connections(network, data):
    """Resolve named OSM regions inside the selected scene, then inspect its graph."""
    origin = data['originXY']
    def local(point):
        return [point[i] - origin[i] for i in (0, 1)]
    scope = Polygon(data['scope'])
    path = ROOT / 'scenarios/shanghai_bund/bund_real.osm.xml'
    regions = []
    for region in _osm_regions(str(path), path.stat().st_mtime_ns):
        polygon = Polygon([local(network.net.convertLonLat2XY(*point)) for point in region['coordinates']])
        if polygon.is_valid and polygon.intersection(scope).area > 1:
            regions.append({**region, 'polygon': polygon})
    parks = [region for region in regions if region['type'] == 'park']
    cores = [region for region in regions if region['type'] == 'monument']
    park = max(parks, key=lambda region: region['polygon'].intersection(scope).area) if parks else None
    core = min(cores, key=lambda region: region['polygon'].centroid.distance(Point(data['center']))) if cores else None
    # The frozen scope is the operative site boundary, including its connected
    # approach paths; park identity comes from OSM, not an invented gate list.
    site = {**(park or {'id': data['locationId'], 'name': '场景区域'}), 'type': 'park', 'polygon': scope}
    node_positions, adjacent = {}, {}
    native_ids = {lane['edgeId'] for lane in data['lanes']}
    for lane in data['lanes']:
        edge = network.edges[lane['edgeId']]
        for node in (edge.getFromNode(), edge.getToNode()):
            node_positions[node.getID()] = local(node.getCoord())
            for other in node.getIncoming() + node.getOutgoing():
                if other.getID() not in native_ids and not other.getID().startswith(':'):
                    adjacent[other.getID()] = other
    external = []
    for edge in adjacent.values():
        paths = [[local(p) for p in lane.getShape()] for lane in edge.getLanes() if lane.allows('pedestrian')]
        if paths:
            external.append({'edgeId': edge.getID(), 'from': edge.getFromNode().getID(),
                             'to': edge.getToNode().getID(), 'paths': paths})
    portals, tolerance = find_region_connections(data['lanes'], node_positions, external, site, core)
    for portal in portals:
        point = portal.pop('point')
        portal['position'] = list(network.xy_to_lonlat(point[0] + origin[0], point[1] + origin[1]))
    return {'portals': portals, 'portalAnalysis': {'method': 'region-topology-v1', 'junctionToleranceM': tolerance,
            'regionIds': [region['id'] for region in (core, site) if region], 'siteBoundary': 'frozen-scene-scope'}}
