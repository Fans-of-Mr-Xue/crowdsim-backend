"""Map-only checks using the real network; no SUMO process or web services."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.decision.position_aware_router import PositionAwarePedestrianRouter
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.scenarios.class2_flow_layout import ROOT, digest, flow_network, position
from crowdsim.scenarios.class2_flow_runtime import Class2FlowController
from crowdsim.scenarios.class2_map_geometry import build_map_geometry
from tests.class2_flow_fixtures import flow_request


@pytest.fixture(scope='module')
def network():
    return ResearchNetwork(str(ROOT / 'scenarios/shanghai_bund/bund.net.xml'))


def fixture(network):
    request = flow_request(count=20)
    design = request['flowDesign']
    flow = design['flows'][0]
    design['flows'].append({**deepcopy(flow), 'id': 'reverse', 'name': '反向人流',
                           'origin': deepcopy(flow['destination']), 'destination': deepcopy(flow['origin'])})
    runtime = SimpleNamespace(run_id='map-test', network=network,
        route_provider=SimpleNamespace(position_router=PositionAwarePedestrianRouter(network)),
        requirement_record={'requirement': {'class2_demo': {'source': request['source']}}})
    return runtime, {'flow_design': design, 'flow_design_sha256': digest(design)}


def test_real_map_exports_frozen_anchors_oriented_routes_and_clipped_edges(network):
    runtime, layout = fixture(network)
    frozen = deepcopy(layout)
    geometry = build_map_geometry(runtime, layout)
    assert layout == frozen
    assert geometry['coordinateSystem'] == 'wgs84'
    assert geometry['runId'] == runtime.run_id
    assert geometry['source']['snapshotId'] == 'snapshot-test'
    assert geometry['flowDesignSha256'] == digest(layout['flow_design'])
    assert geometry['networkSha256'] == flow_network()['sourceSha256']
    lane = next(lane for lane in flow_network()['lanes'] if lane['id'] == layout['flow_design']['flows'][0]['origin']['lane_id'])
    origin = position(lane, 25)
    assert geometry['flows'][0]['origin'] == list(network.xy_to_lonlat(origin['x'], origin['y']))
    forward, reverse = [flow['segments'][0] for flow in geometry['flows']]
    assert forward['direction'] == 'forward'
    assert reverse['direction'] == 'reverse'
    assert forward['path'] == reverse['path'][::-1]
    assert forward['path'][0] == pytest.approx(geometry['flows'][0]['origin'], abs=1e-7)
    assert forward['path'][-1] == pytest.approx(geometry['flows'][0]['destination'], abs=1e-7)
    assert set(row['edgeId'] for row in geometry['edges']) == {lane['edgeId'] for lane in flow_network()['lanes']}
    assert len(json.dumps(geometry)) < 100000


def test_multiedge_route_stays_connected_and_each_segment_has_true_direction(network):
    runtime, layout = fixture(network)
    lanes = flow_network()['lanes']
    layout['flow_design']['flows'][0]['origin'] = {'lane_id': lanes[0]['id'], 'offset': lanes[0]['length'] / 2}
    result = build_map_geometry(runtime, layout)['flows'][0]
    assert len(result['segments']) > 1
    # Junctions have physical width; the existing navigation builder connects
    # the neighbouring edge endpoints across that width in the complete path.
    flattened = []
    for segment in result['segments']:
        for point in segment['path']:
            if not flattened or point != flattened[-1]:
                flattened.append(point)
    assert flattened == result['path']
    assert result['segments'][0]['path'][0] == pytest.approx(result['origin'], abs=1e-7)
    assert result['segments'][-1]['path'][-1] == pytest.approx(result['destination'], abs=1e-7)


def test_geometry_is_cached_per_controller_and_added_only_to_class_two_init(network):
    runtime, layout = fixture(network)
    controller = object.__new__(Class2FlowController)
    controller.layout, controller._map_geometry = layout, None
    result = controller.map_geometry(runtime)
    assert controller.map_geometry(runtime) is result
    runtime.class2_flows = SimpleNamespace(map_geometry=lambda value: result, control=SimpleNamespace(plan=None))
    runtime.serializer = SimpleNamespace(build_init=lambda _: {'type': 'init'})
    runtime.generated_demand_spec = SimpleNamespace(is_class2_demo=True)
    runtime.artificial_society_diagnostics = lambda: {}
    runtime.performance = SimpleNamespace(metadata=lambda: {})
    assert SimulationRuntime.init_frame(runtime)['class2_map_geometry'] is result
    runtime.class2_flows = None
    runtime.generated_demand_spec.is_class2_demo = False
    assert SimulationRuntime.init_frame(runtime) == {'type': 'init', 'artificial_society': {}, 'performance_measurement': {}}
