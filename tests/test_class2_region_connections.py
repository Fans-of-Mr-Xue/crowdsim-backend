"""Region-connection parsing only; no web services, model or SUMO process."""
from collections import Counter
from copy import deepcopy

import pytest
from shapely.geometry import Polygon

from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.scenarios.class2_flow_layout import ROOT, flow_network
from crowdsim.scenarios.class2_region_connections import find_region_connections, scene_region_connections


def fixture():
    nodes = {'w-in': (-4, 0), 'w-out': (-12, 0), 'e-in': (4, 0), 'e-out': (12, 0),
             's-in': (0, -4), 's-out': (0, -12), 'site-e': (19, 0), 'site-n': (0, 10)}
    def lane(edge, a, b):
        return {'edgeId': edge, 'from': a, 'to': b, 'width': 1, 'path': [nodes[a], nodes[b]]}
    lanes = [lane('west', 'w-in', 'w-out'), lane('east', 'e-in', 'e-out'), lane('south', 's-in', 's-out'),
             lane('internal', 'w-in', 'e-in'), lane('approach', 'e-out', 'site-e'),
             {'edgeId': 'cut', 'from': 'site-n', 'to': 'virtual-cut', 'width': 1,
              'path': [(0, 10), (0, 19)], 'sourceStart': 0, 'sourceEnd': 9, 'sourceShapeLength': 20}]
    external = [{'edgeId': 'outside', 'from': 'site-e', 'to': 'other', 'paths': [[(19, 0), (30, 0)]]}]
    site = {'id': 'site', 'name': '测试公园', 'type': 'park', 'polygon': Polygon([(-20, -20), (20, -20), (20, 20), (-20, 20)])}
    monument = {'id': 'core', 'name': '测试地标', 'type': 'monument', 'polygon': Polygon([(-4, -4), (4, -4), (4, 4), (-4, 4)])}
    return lanes, nodes, external, site, monument


def test_connections_are_derived_from_regions_and_topology_not_an_expected_count():
    args = fixture()
    result, tolerance = find_region_connections(*args)
    assert tolerance == 3
    assert Counter(row['regionType'] for row in result) == {'monument': 3, 'park': 2}
    assert {tuple(row['point']) for row in result if row['regionType'] == 'monument'} == {(-8, 0), (8, 0), (0, -8)}
    assert next(row for row in result if 'cut' in row['edgeIds'])['point'] == [0, 19]
    args[0].pop(0)
    result, _ = find_region_connections(*args)
    assert sum(row['regionType'] == 'monument' for row in result) == 2


def test_parallel_reverse_roads_are_one_connection_and_internal_dead_ends_are_not_gates():
    args = fixture()
    reverse = {**args[0][0], 'edgeId': 'west-reverse', 'from': 'w-out', 'to': 'w-in', 'path': [(-12, 0), (-4, 0)]}
    args[0].append(reverse)
    result, _ = find_region_connections(*args)
    west = [row for row in result if 'west' in row['edgeIds']]
    assert len(west) == 1
    assert west[0]['edgeIds'] == ['west', 'west-reverse']
    assert all('internal' not in row['edgeIds'] for row in result)


def test_geometric_crossings_without_topology_and_nonwalkable_external_roads_are_rejected():
    lanes, nodes, _, site, core = fixture()
    external = [
        {'edgeId': 'bridge', 'from': 'bridge-a', 'to': 'bridge-b', 'paths': [[(19, 0), (30, 0)]]},
        {'edgeId': 'vehicles-only', 'from': 'site-e', 'to': 'road', 'walkable': False, 'paths': [[(19, 0), (30, 0)]]},
        {'edgeId': 'internal-service', 'from': 'site-e', 'to': 'inside', 'paths': [[(19, 0), (19, 5)]]},
    ]
    result, _ = find_region_connections(lanes, nodes, external, site, core)
    assert [row['edgeIds'] for row in result if row['regionType'] == 'park'] == [['cut']]


def test_translating_and_scaling_a_scene_moves_its_connections_without_coordinate_overrides():
    args = fixture()
    baseline, _ = find_region_connections(*args)
    lanes, nodes, external, site, core = deepcopy(args)
    transform = lambda p: (p[0] * 2 + 73, p[1] * 2 - 51)
    for lane in lanes:
        lane['path'] = [transform(p) for p in lane['path']]
        lane['width'] *= 2
        for key in ('sourceStart', 'sourceEnd', 'sourceShapeLength'):
            if key in lane:
                lane[key] *= 2
    nodes = {node: transform(p) for node, p in nodes.items()}
    for edge in external:
        edge['paths'] = [[transform(p) for p in path] for path in edge['paths']]
    for region in (site, core):
        region['polygon'] = Polygon([transform(p) for p in region['polygon'].exterior.coords])
    actual, _ = find_region_connections(lanes, nodes, external, site, core)
    by_id = {row['id']: row for row in actual}
    assert len(actual) == len(baseline)
    for row in baseline:
        assert by_id[row['id']]['point'] == pytest.approx(transform(row['point']))


def test_real_scene_finds_the_three_short_tower_connectors_and_four_external_park_connections():
    network = ResearchNetwork(str(ROOT / 'scenarios/shanghai_bund/bund.net.xml'))
    data = flow_network()
    result = scene_region_connections(network, data)
    assert result['portalAnalysis']['method'] == 'region-topology-v1'
    assert Counter(row['regionType'] for row in result['portals']) == {'monument': 3, 'park': 4}
    tower_edges = {edge for row in result['portals'] if row['regionType'] == 'monument' for edge in row['edgeIds']}
    assert tower_edges == {'177931018#0', '178411801#0', 'monument_m3'}
    native_ids = {lane['edgeId'] for lane in data['lanes']}
    for row in result['portals']:
        assert 121.48 < row['position'][0] < 121.49 and 31.24 < row['position'][1] < 31.25
        assert native_ids.intersection(row['edgeIds'])
        assert row['method']
    assert scene_region_connections(network, data) == result
