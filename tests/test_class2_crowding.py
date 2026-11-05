"""Class-two flow generation checks; the fixed departure demo has been replaced."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock
import xml.etree.ElementTree as ET
import pytest
import traci.constants as tc
from crowdsim.domain.crowdsim_models import MotionSnapshot
from crowdsim.scenarios.class2_crowding import (
    ROOT, PRESET_PATH, PRESET_ID, Class2CrowdingDemandSpec, load_preset,
    prepare_requirement, preset_capabilities, validate_binding,
)
from crowdsim.scenarios.class2_flow_layout import build_flow_layout, validate_design
from crowdsim.scenarios.class2_flow_runtime import Class2FlowController
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.sumo_adapter import SumoStepResult
from crowdsim_overlay_server import build_requirement_runtime, build_runtime, parse_args
from tests.class2_flow_fixtures import flow_request


def test_no_implicit_fixed_crowd_or_old_capacity_limit():
    capabilities = preset_capabilities()
    assert capabilities['id'] == PRESET_ID
    assert capabilities['requiresFlowDesign']
    assert capabilities['countMax'] == 10000
    assert 'defaultCount' not in capabilities
    request = flow_request(); del request['flowDesign']
    with pytest.raises(ValueError, match='人流配置'):
        prepare_requirement(request)


def test_execution_preserves_snapshot_inputs_and_rejects_tampering():
    request = flow_request()
    frozen = deepcopy(request)
    spec = prepare_requirement(request)
    assert request == frozen
    binding = validate_binding(spec.payload)
    assert binding['flow_design'] == request['flowDesign']
    assert spec.payload['population']['distributions'] == request['populationDistributions']
    assert spec.payload['observation']['metric_ids'] == request['metricIds']
    for key in ['flow_design_sha256', 'layout_sha256', 'map_fingerprint']:
        broken = deepcopy(spec.payload); broken['class2_demo'][key] = 'invalid'
        with pytest.raises(ValueError):
            validate_binding(broken)


@pytest.mark.parametrize('change', [
    {'count': 0}, {'mapFingerprint': 'unknown'}, {'source': None}, {'metricIds': []},
    {'durationSeconds': 0}, {'problemType': 1},
])
def test_invalid_requests_are_rejected(change):
    with pytest.raises(ValueError):
        prepare_requirement(flow_request(**change))


def test_incomplete_or_invalid_points_never_fall_back_to_fixed_layout():
    for value in [None, {'lane_id': 'made-up', 'offset': 10}, {'lane_id': '679361567#1_0:0', 'offset': -1}]:
        request = flow_request(); request['flowDesign']['flows'][0]['origin'] = value
        with pytest.raises(ValueError):
            prepare_requirement(request)


@pytest.mark.parametrize('count', [1, 100, 300, 600, 700, 1000])
def test_demand_has_exact_flow_membership_and_requested_destinations(tmp_path, count):
    request = flow_request(count)
    spec = prepare_requirement(request)
    preset, fingerprint, _ = load_preset()
    demand = Class2CrowdingDemandSpec(ROOT/preset['source_demand'], PRESET_PATH,
        bound_count=count, flow_design=spec.payload['class2_demo']['flow_design'], expected_fingerprint=fingerprint)
    output, report = demand.generate(tmp_path, ROOT/preset['network'], count)
    people = ET.parse(output).getroot().findall('person')
    layout = json.loads((tmp_path/'class2_initial_layout.json').read_text())
    assert len(people) == len(layout['positions']) == count
    assert report['flow_counts'] == {'user-flow': count}
    assert all(p.find("param[@key='crowdsim.flow_id']").get('value') == 'user-flow' for p in people)
    for p, planned in zip(people, layout['positions']):
        walk = p.find('walk')
        assert walk.get('edges').split()[-1] == planned['destination']['edge']
        assert abs(float(walk.get('arrivalPos'))-planned['destination']['position']) < 1e-8
        assert len(p.findall('walk')) == 2  # Arrival + dormant position-retention stage.
    assert layout['flows'][0]['origin_layout']['area'] == pytest.approx(count/2.2, abs=1e-5)


def test_first_class_runtime_keeps_original_generator_and_configuration(tmp_path):
    original = build_runtime(parse_args(['--scenario', 'hotspot']))
    record = RequirementRepository(tmp_path).create(prepare_requirement(flow_request()), purpose='experiment')
    second = build_requirement_runtime(parse_args(['--scenario', 'hotspot']), record)
    try:
        assert not getattr(original.generated_demand_spec, 'is_class2_demo', False)
        assert original.class2_flows is None
        assert second.generated_demand_spec.is_class2_demo
        assert second.generated_demand_spec.flow_design == record['requirement']['class2_demo']['flow_design']
        assert original.hotspot_demand_spec.config_path != second.hotspot_demand_spec.config_path
    finally:
        original.close(); second.close()


def test_destination_stage_transition_retains_person_when_position_sample_misses_target(tmp_path):
    layout = build_flow_layout(flow_request(1)['flowDesign'])
    layout_file = tmp_path / 'layout.json'
    layout_file.write_text(json.dumps(layout))
    controller = Class2FlowController(layout_file)
    person, = layout['positions']
    pid, target = person['person_id'], person['destination']
    motion = MotionSnapshot(
        person_id=pid, time_seconds=10, x=0, y=0, lon=0, lat=0, speed=.5, edge_id=target['edge'],
        lane_id=target['source_lane_id'], lane_position=target['position'] - .25, angle=0,
        stage_index=0, stage_type=tc.STAGE_WALKING, remaining_stage_count=3,
    )
    approaching = SumoStepResult(10, {pid: motion}, {}, (), ())
    in_retention_walk = replace(approaching, persons={pid: replace(motion, remaining_stage_count=2)})
    runtime = SimpleNamespace(
        adapter=Mock(), plan_executor=Mock(),
        population=SimpleNamespace(states={pid: SimpleNamespace(activity_state='walking')}),
        recorder=None,
    )
    controller.update(runtime, approaching)
    assert not controller.dwelling
    runtime.adapter.move_person_on_route.assert_not_called()

    controller.update(runtime, in_retention_walk)
    runtime.adapter.move_person_on_route.assert_called_once_with(
        pid, target['edge'], target['x'], target['y'], motion.angle,
    )
    runtime.adapter.replace_current_person_stage.assert_not_called()
    runtime.plan_executor.hold_activity.assert_called_once_with(pid, None)
    assert not controller.dwelling and pid in controller.retention_pending

    stopped = replace(motion, lane_position=target['position'], speed=0, remaining_stage_count=2)
    controller.update(runtime, replace(in_retention_walk, persons={pid: stopped}))
    assert controller.dwelling == {pid}
    assert not controller.retention_pending
    assert runtime.population.states[pid].activity_state == 'class2_dwelling'
