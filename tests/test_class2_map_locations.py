"""Map tool tests call pure services only: no HTTP listener, DB or SUMO process."""
from copy import deepcopy
from unittest.mock import patch

import pytest
from shapely.geometry import LineString, Point, Polygon

from crowdsim.scenarios.class2_flow_layout import flow_network
from crowdsim.scenarios.class2_map_locations import MemorialMapTool, _geometry


@pytest.fixture
def sample():
    service = MemorialMapTool()
    body = {'locationId': 'memorial-tower', 'networkSha256': flow_network()['sourceSha256'],
            'source': {'conversationId': 'map-test', 'draftId': 'draft-test', 'revision': 4}}
    results = service.resolve({**body, 'queries': [
        {'key': 'a.origin', 'description': '纪念塔西侧步道'},
        {'key': 'a.destination', 'description': '纪念塔南侧步道'}]})['results']
    a, b = [r['candidates'][0] for r in results]
    design = {'version': 1, 'location_id': 'memorial-tower', 'network_sha256': body['networkSha256'], 'flows': [
        {'id': 'a', 'name': '西向南', 'count': 200, 'density': 2, 'speed': 1.3, 'origin': a['anchor'], 'destination': b['anchor']}]}
    request = {**body, 'design': design, 'pointRefs': [
        {'flowId': 'a', 'kind': 'origin', 'ref': a['ref']}, {'flowId': 'a', 'kind': 'destination', 'ref': b['ref']}]}
    return service, body, results, request


def test_real_walkable_candidates_and_200_person_layout_stay_on_the_map(sample):
    service, body, results, request = sample
    with patch('subprocess.Popen', side_effect=AssertionError('No services or simulator allowed')):
        context = service.context(body)
        result = service.preview(request)
    assert context['walkableParts'] == 51
    assert result['valid'], result['issues']
    assert request['design']['flows'][0]['count'] == 200
    check = result['checks'][0]
    assert check['requiredAreaM2'] == 100
    assert check['originAreaM2'] == pytest.approx(100, abs=.001)
    assert check['destinationAreaM2'] == pytest.approx(100, abs=.001)
    route = result['routes'][0]
    assert route['verifiedPeople'] == 200 and route['distanceM'] > 0
    assert Polygon(flow_network()['scope']).buffer(.1).covers(LineString(route['path']))
    center = context['landmarks'][0]['center']
    assert results[0]['candidates'][0]['point'][0] < center[0]
    assert results[1]['candidates'][0]['point'][1] < center[1]
    for group in results:
        assert len(group['candidates']) == 3
        assert all(Polygon(flow_network()['scope']).covers(Point(c['point'])) for c in group['candidates'])


@pytest.mark.parametrize('description', ['纪念塔西门', '博物馆地下入口', '南京东路', '江边', '不存在的咖啡馆', '世纪公园东侧'])
def test_unknown_or_unverified_places_do_not_turn_into_invented_markers(sample, description):
    service, body, _, _ = sample
    row = service.resolve({**body, 'queries': [{'key': 'a', 'description': description}]})['results'][0]
    assert row['candidates'] == [] and row['reason']


@pytest.mark.parametrize('landmark,index', [('纪念塔', 0), ('黄浦公园', 1)])
@pytest.mark.parametrize('direction', ['东', '西', '南', '北', '东北', '西北', '东南', '西南'])
def test_compass_queries_return_candidates_in_the_requested_half_plane(sample, landmark, index, direction):
    service, body, _, _ = sample
    row = service.resolve({**body, 'queries': [{'key': 'a', 'description': landmark+direction+'侧'}]})['results'][0]
    center = service.context(body)['landmarks'][index]['center']
    assert row['candidates']
    for candidate in row['candidates']:
        x, y = candidate['point']
        if '东' in direction: assert x >= center[0]
        if '西' in direction: assert x <= center[0]
        if '北' in direction: assert y >= center[1]
        if '南' in direction: assert y <= center[1]


@pytest.mark.parametrize('field,value', [('conversationId', 'other'), ('draftId', 'other'), ('revision', 5)])
def test_candidate_references_cannot_cross_conversations_or_revisions(sample, field, value):
    service, _, _, request = sample
    request['source'][field] = value
    result = service.preview(request)
    assert not result['valid'] and '候选点' in result['issues'][0]


def test_version_tampering_capacity_overflow_and_foreign_anchors_fail(sample):
    service, body, _, request = sample
    with pytest.raises(ValueError): service.context({**body, 'networkSha256': 'old'})
    with pytest.raises(ValueError): service.context({**body, 'locationId': 'another-park'})
    bad = deepcopy(request)
    bad['design']['flows'][0]['origin']['offset'] += .5
    assert not service.preview(bad)['valid']
    bad = deepcopy(request)
    bad['design']['flows'][0].update(count=10000, density=.1)
    assert not service.preview(bad)['valid']
    service.candidates[request['pointRefs'][0]['ref']]['expires'] = 0
    assert not service.preview(request)['valid']


def test_disconnected_routes_are_not_accepted_as_straight_lines(sample):
    service, _, _, request = sample
    router = _geometry(flow_network()['geometrySha256'])[2]
    with patch.object(router, 'route', side_effect=ValueError('无可达步行路线')):
        result = service.preview(request)
    assert not result['valid'] and result['routes'] == []


def test_distance_constraints_are_preserved_instead_of_silently_snapping_far_away(sample):
    service, body, _, _ = sample
    close = service.resolve({**body, 'queries': [{'key': 'a', 'description': '纪念塔西侧20米'}]})['results'][0]
    assert close['candidates']
    center = service.context(body)['landmarks'][0]['center']
    goal = Point(center[0]-20, center[1])
    assert all(goal.distance(Point(c['point'])) <= 12 for c in close['candidates'])
    far = service.resolve({**body, 'queries': [{'key': 'a', 'description': '纪念塔西侧100米'}]})['results'][0]
    assert far['candidates'] == []


@pytest.mark.parametrize('direction,delta', [('东',(20,0)),('西',(-20,0)),('南',(0,-20)),('北',(0,20))])
def test_park_distance_queries_use_the_same_center_as_the_map_context(sample, direction, delta):
    service, body, _, _ = sample
    center = service.context(body)['landmarks'][1]['center']
    goal = Point(center[0]+delta[0],center[1]+delta[1])
    row = service.resolve({**body,'queries':[{'key':'p','description':'黄浦公园'+direction+'侧20米'}]})['results'][0]
    assert row['candidates']
    assert all(goal.distance(Point(candidate['point'])) <= 12 for candidate in row['candidates'])


@pytest.mark.parametrize('description,index,delta', [('纪念塔东侧约20米的步道', 0, (20, 0)),
    ('纪念塔东侧大约20米', 0, (20, 0)), ('纪念塔东侧20米左右的步道', 0, (20, 0)),
    ('黄浦公园中心西侧约20米的步道', 1, (-20, 0))])
def test_approximate_distance_wording_retains_the_requested_target(sample, description, index, delta):
    service, body, _, _ = sample
    center = service.context(body)['landmarks'][index]['center']
    goal = Point(center[0]+delta[0], center[1]+delta[1])
    row = service.resolve({**body, 'queries': [{'key': 'p', 'description': description}]})['results'][0]
    assert row['candidates']
    for candidate in row['candidates']:
        assert goal.distance(Point(candidate['point'])) <= 12
        assert '20米' in candidate['label']


@pytest.mark.parametrize('origin,destination', [
    ('纪念塔东侧', '纪念塔西侧'), ('纪念塔东北侧', '纪念塔西南侧'),
    ('纪念塔东侧20米', '纪念塔西侧20米'), ('黄浦公园西侧', '黄浦公园东侧'),
    ('黄浦公园南侧', '黄浦公园北侧'), ('黄浦公园西南侧', '黄浦公园东北侧'),
    ('黄浦公园南侧', '纪念塔北侧'), ('黄浦公园东侧', '纪念塔西侧'),
    ('黄浦公园西北侧', '纪念塔东南侧'), ('黄浦公园西侧20米', '黄浦公园北侧20米'),
])
def test_every_returned_candidate_pair_has_walkable_routes_and_enough_area(sample, origin, destination):
    service, body, _, request = sample
    rows = service.resolve({**body, 'queries': [{'key': 'a', 'description': origin},
        {'key': 'b', 'description': destination}]})['results']
    assert all(row['candidates'] for row in rows)
    scope = Polygon(flow_network()['scope']).buffer(.1)
    for a in rows[0]['candidates']:
        for b in rows[1]['candidates']:
            trial = deepcopy(request)
            trial['design']['flows'][0].update(count=40, origin=a['anchor'], destination=b['anchor'])
            trial['pointRefs'][0]['ref'] = a['ref']
            trial['pointRefs'][1]['ref'] = b['ref']
            result = service.preview(trial)
            assert result['valid'], (origin, destination, result['issues'])
            route = result['routes'][0]
            assert route['verifiedPeople'] == 40 and route['distanceM'] > 0
            assert scope.covers(LineString(route['path']))
            check = result['checks'][0]
            assert check['requiredAreaM2'] == 20
            assert check['originAreaM2'] == pytest.approx(20, abs=.001)
            assert check['destinationAreaM2'] == pytest.approx(20, abs=.001)


def test_navigation_uses_native_walking_routes_without_population_or_decision_inputs(sample):
    service, body, _, request = sample
    expected = service.preview(request)['routes'][0]
    draft = deepcopy(request['design'])
    draft['flows'][0].update(count=None, density=None, speed=None)
    with patch('subprocess.Popen', side_effect=AssertionError('No services or simulator allowed')):
        result = service.navigation({**body, 'design': draft})
    assert not result['issues']
    assert len(result['routes']) == 1
    assert result['routes'][0]['path'] == expected['path']
    assert result['routes'][0]['distanceM'] == expected['distanceM']
    assert 'verifiedPeople' not in result['routes'][0]
    assert draft['flows'][0]['count'] is None


def test_navigation_keeps_other_flows_when_one_is_incomplete_or_unreachable(sample):
    service, body, _, request = sample
    draft = deepcopy(request['design'])
    draft['flows'].extend([{**deepcopy(draft['flows'][0]), 'id': 'incomplete', 'destination': None},
                          {**deepcopy(draft['flows'][0]), 'id': 'invalid', 'origin': {'lane_id': 'unknown', 'offset': 0}}])
    result = service.navigation({**body, 'design': draft})
    assert [route['flowId'] for route in result['routes']] == ['a']
    assert len(result['issues']) == 1
    assert '路网' in result['issues'][0]
    router = _geometry(flow_network()['geometrySha256'])[2]
    with patch.object(router, 'route', side_effect=ValueError('无可达步行路线')):
        result = service.navigation(request)
    assert result['routes'] == [] and '无可达' in result['issues'][0]


def test_navigation_rejects_stale_geometry_and_duplicate_flow_ids(sample):
    service, body, _, request = sample
    with pytest.raises(ValueError): service.navigation({**request, 'locationId': 'elsewhere'})
    with pytest.raises(ValueError): service.navigation({**request, 'networkSha256': 'old'})
    stale = deepcopy(request)
    stale['design']['network_sha256'] = 'old'
    with pytest.raises(ValueError): service.navigation(stale)
    duplicate = deepcopy(request)
    duplicate['design']['flows'] *= 2
    with pytest.raises(ValueError): service.navigation(duplicate)
    assert service.navigation({**body, 'design': {**request['design'], 'flows': []}})['routes'] == []
