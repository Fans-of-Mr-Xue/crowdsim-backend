import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from shapely.geometry import LineString

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot
from crowdsim.scenarios.class2_local_pedestrian import LocalPedestrianController, LocalPedestrianModel, parse_choice


@pytest.mark.parametrize('value', [
    {'action': 'change_goal', 'option_id': None, 'reason': '离开'},
    {'action': 'choose_path', 'option_id': 'invented-road', 'reason': '换路'},
    {'action': 'continue', 'option_id': 'option-1', 'reason': '继续'},
    {'action': 'wait', 'option_id': None, 'reason': '等', 'seconds': 100},
])
def test_rejects_actions_outside_pedestrian_contract(value):
    with pytest.raises(ValueError):
        parse_choice(value, {'option-1'})


def test_model_failure_keeps_original_route(monkeypatch):
    model = LocalPedestrianModel()
    async def fail(messages):
        raise TimeoutError()
    monkeypatch.setattr(model, '_request', fail)
    value, error = asyncio.run(model.choose({'local_options': []}))
    assert value['action'] == 'continue' and value['option_id'] is None
    assert error == 'TimeoutError' and model.fallbacks == 1


def local_controller():
    # Geometry deliberately crosses the radius boundary and a nearby person is
    # on another edge. Distant population changes must not affect model input.
    own = MotionSnapshot('self', 10, 0, 0, 0, 0, 1, 'a', 'a_0', 1, 0, 0, 2)
    nearby = replace(own, person_id='nearby', edge_id='b', x=2)
    far = replace(own, person_id='secret', x=100)
    runtime = SimpleNamespace(
        time_seconds=10, current=SimpleNamespace(persons={'self': own, 'nearby': nearby, 'secret': far}),
        profile_for=lambda pid: AgentProfile(pid, perception_radius=6),
        population=SimpleNamespace(states={'self': AgentState('self')}),
    )
    c = object.__new__(LocalPedestrianController)
    c.runtime = runtime
    c.flows = SimpleNamespace(speeds={'self': 1})
    c.final_targets = {'self': {'x': 1000, 'y': 0}}
    c.previous = {}
    c.lanes = [({'width': 2}, LineString([(-20, 0), (20, 0)]))]
    c._options = lambda *args: ([], {})
    return c


def test_context_is_local_cross_edge_and_hides_private_intents():
    controller = local_controller()
    context, _ = controller.observe('self')
    assert len(context['nearby_people']) == 1
    assert context['nearby_people'][0]['relative_xy_m'] == [2, 0]
    assert set(context['nearby_people'][0]) == {'relative_xy_m', 'speed_mps', 'heading_degrees'}
    assert max(abs(p[0]) for p in context['local_roads'][0]['relative_path_m']) <= 6
    assert set(context['goal']) == {'direction_east_north', 'instruction'}
    controller.runtime.current.persons['secret'] = replace(controller.runtime.current.persons['secret'], x=200, speed=0)
    assert controller.observe('self')[0] == context


def test_arrived_and_background_people_are_not_requested_and_stale_results_are_discarded():
    c = local_controller()
    c.ids = ['self', 'arrived']  # background never enters the activity cohort
    c.next_round = 10
    c.concurrency = 2
    c.history = []
    c.runtime.snapshot_id = 'before'
    c.eligible = lambda pid: pid == 'self'
    calls = []
    async def choose(context):
        calls.append(deepcopy(context))
        c.runtime.snapshot_id = 'after'
        return {'action': 'continue', 'option_id': None, 'reason': '继续'}, None
    c.model = SimpleNamespace(choose=choose)
    records = asyncio.run(c.decide_round())
    assert len(calls) == 1
    assert records[0]['execution']['status'] == 'discarded'
    assert c.next_round == 20
    assert asyncio.run(c.decide_round()) == []


@pytest.mark.parametrize('background', [None, 0, 2])
def test_real_sumo_runtime_enables_only_new_class2_and_controls_only_activity(tmp_path, monkeypatch, background):
    from crowdsim.scenarios.class2_crowding import prepare_requirement
    from crowdsim.scenarios.class2_society import publish_class2_society
    from crowdsim.infrastructure.requirement_repository import RequirementRepository
    from dialog_storage.during_requirements import DuringRequirementRepository
    from crowdsim_overlay_server import build_requirement_runtime, parse_args
    from tests.class2_flow_fixtures import flow_request
    from tests.test_during_workflow import confirmed

    monkeypatch.setenv('CROWDSIM_RUNS_ROOT', str(tmp_path / 'runs'))
    body = flow_request(2, durationSeconds=600)
    if background is not None:
        body.update(backgroundCount=background, count=2 + background)
    record = RequirementRepository(tmp_path / 'requirements').create(prepare_requirement(body), purpose='experiment')
    state = confirmed(2)
    r = state['requirement']['draft']['requirement']
    r.update(population=deepcopy(record['requirement']['population']),
             experiment={'duration_seconds': 600}, observation={'metric_ids': body['metricIds']},
             class2={'mode': 'automated_context' if background is not None else 'legacy',
                     'flow_design': body['flowDesign'], 'background_population': {'count': background or 0}})
    state['requirement']['draft']['savedSnapshotId'] = body['source']['snapshotId']
    state['requirement']['draft']['confirmation']['id'] = body['source']['snapshotId']
    state['requirement']['snapshots'][0].update(id=body['source']['snapshotId'], requirement=deepcopy(r))
    repository = DuringRequirementRepository(tmp_path / 'conversation')
    repository.save(body['source']['conversationId'], state=state, expected_revision=0, mutation_id='confirm')
    publish_class2_society({**record, 'class2_demo': record['requirement']['class2_demo']}, requirements=repository)
    calls = []
    async def choose(self, context):
        calls.append(context)
        return {'action': 'wait', 'option_id': None, 'reason': '测试暂停后恢复'}, None
    monkeypatch.setattr(LocalPedestrianModel, 'choose', choose)
    runtime = build_requirement_runtime(parse_args(['--scenario', 'hotspot']), record)
    runtime.archive_root = tmp_path / 'runs'
    try:
        runtime.initialize()
        controller = runtime.class2_local_pedestrians
        assert (controller is not None) == (background is not None)
        if controller:
            assert len(controller.ids) == 2
            assert not set(controller.ids) & runtime.class2_flows.background_ids
        runtime.start()
        asyncio.run(runtime.tick_async())
        if controller:
            assert len(calls) == 2
            assert all(runtime.class2_flows.confirmed_speeds[pid] == 0 for pid in controller.ids)
            runtime.pause()
            runtime.start()
            asyncio.run(runtime.tick_async())
            assert len(calls) == 2  # Resume does not repeat the same ten-second round.
            assert all(runtime.population.states[pid].current_plan.source == 'local_llm' for pid in controller.ids)
            assert all(runtime.population.states[pid].current_plan.source != 'local_llm'
                       for pid in runtime.class2_flows.background_ids)
        else:
            assert not calls
    finally:
        runtime.close()
