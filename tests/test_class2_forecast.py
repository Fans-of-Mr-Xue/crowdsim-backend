"""Offline contracts for second-class evidence and isolated preview lifecycle."""
import asyncio
from copy import deepcopy
import json
from functools import wraps
from pathlib import Path
from types import SimpleNamespace as NS

from crowdsim.core.simulation_runtime import RuntimeState
from crowdsim.domain.crowdsim_models import AgentState
from crowdsim.infrastructure.websocket_server import OverlayServer
from crowdsim.scenarios.class2_control_context import summarize_paths
from crowdsim.scenarios.class2_forecast import BaselineCollector, Class2ForecastService
from crowdsim.scenarios.class2_strategy_tool import strategy_tool, ACTIONS


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def fixture():
    people = {'a': {'flow_id': 'f1', 'destination': {'edge': 'road', 'position': 100}},
              'b': {'flow_id': 'f2', 'destination': {'edge': 'road', 'position': 0}}}
    motions = {pid: NS(edge_id='road', lane_position=pos, x=pos, y=0, speed=0)
               for pid, pos in [('a', 80), ('b', 20)]}
    flows = NS(people=people, dwelling=set(), retention_pending={}, speeds={'a': 1, 'b': 1},
        layout={'flows': [{'id': 'f1'}, {'id': 'f2'}]},
        metrics=lambda: {'flows': [{'id': 'f1'}, {'id': 'f2'}]})
    edge = NS(getFromNode=lambda: NS(getID=lambda: 'start'), getToNode=lambda: NS(getID=lambda: 'end'), getLength=lambda: 100)
    return NS(time_seconds=.5, step_length=.5, timeline_end_seconds=20, state=RuntimeState.READY,
        current=NS(persons=motions), class2_flows=flows, network=NS(edges={'road': edge}),
        adapter=NS(current_person_stage=lambda pid: NS(edges=['road'])),
        population=NS(states={pid: AgentState(pid) for pid in people}),
        plan_executor=NS(flow_hold_reasons={}, activity_hold_until={}),
        visual_policy=NS(low_speed_threshold_mps=.2, confirmation_duration_seconds=5))


def test_same_road_opposite_directions_distinguish_separation_from_approach():
    runtime = fixture()
    result = summarize_paths(runtime, runtime.class2_flows)
    assert result['predictedOverlaps']  # Co-presence alone is deliberately retained as a reference.
    relation = result['currentFlowRelations'][0]
    assert relation['relation'] == 'separating'
    assert relation['longitudinalGapM'] == 60
    assert relation['positionRangesM'] == [[80, 80], [20, 20]]
    assert result['currentRoadObservations'][0]['meanTravelSpeedMps'] is None
    runtime.current.persons['a'].lane_position = 20
    runtime.current.persons['b'].lane_position = 80
    assert summarize_paths(runtime, runtime.class2_flows)['currentFlowRelations'][0]['relation'] == 'approaching'
    assert runtime.current.persons['a'].lane_position == 20  # Evidence does not rewrite motion.


def test_baseline_contact_is_spatial_and_initial_zero_speed_is_not_queueing():
    runtime = fixture()
    collector = BaselineCollector(runtime)
    collector.sample(runtime)
    road = collector.result(runtime)['roads'][0]
    assert road['sharedRoadIntervals'][0]['intervals'] == [[.5, .5]]
    assert road['nearCrossFlowIntervals'] == []
    assert road['slowWaitingPeak'] == 0 and road['bins'][0][4] is None
    runtime.state = RuntimeState.RUNNING
    runtime.current.persons['b'].x = 81
    for i in range(1, 11):
        runtime.time_seconds = .5+i*.5
        for state in runtime.population.states.values():
            state.low_speed_duration = i*.5
        collector.sample(runtime)
    road = collector.result(runtime)['roads'][0]
    assert road['nearCrossFlowIntervals'][0]['intervals'] == [[1, 5.5]]
    assert road['slowWaitingPeak'] == 2
    assert road['bins'][0][2] == [1, 1]
    assert collector.result(runtime)['stepSeconds'] == .5
    assert collector.result(runtime)['binSeconds'] == 10


def test_destination_stops_and_explicit_holds_do_not_become_queue_references():
    runtime = fixture()
    runtime.class2_flows.dwelling.add('a')
    runtime.plan_executor.flow_hold_reasons['b'] = {'entry'}
    runtime.state = RuntimeState.RUNNING
    collector = BaselineCollector(runtime)
    for i in range(12):
        runtime.time_seconds = .5+i*.5
        for state in runtime.population.states.values():
            state.low_speed_duration = 99
        collector.sample(runtime)
    road = collector.result(runtime)['roads'][0]
    assert road['slowWaitingPeak'] == 0
    assert road['bins'][0][-1] == 1


def test_sparse_bins_include_zero_occupancy_and_keep_personal_destinations():
    runtime = fixture()
    collector = BaselineCollector(runtime)
    before = deepcopy(runtime.class2_flows.people)
    collector.sample(runtime)
    for motion in runtime.current.persons.values():
        motion.edge_id = 'other'
    runtime.time_seconds = 1
    collector.sample(runtime)
    road = next(r for r in collector.result(runtime)['roads'] if r['edgeId'] == 'road')
    assert road['bins'][0][2] == [.5, .5]
    assert collector.result(runtime)['populationConserved']
    assert runtime.class2_flows.people == before


def test_effect_contract_exposes_message_only_guidance_and_speed_only_reduction():
    tool = strategy_tool(ACTIONS, {'F1': 'flow'})
    assert set(tool['effects']) == ACTIONS
    assert '仅发送和记录消息' in tool['effects']['publish_guidance']
    assert '不改变路宽或物理容量' in tool['effects']['set_edge_capacity']
    assert '只记录观测' in tool['effects']['observe_only']


@async_test
async def test_worker_is_serialized_cached_and_scoped_to_frozen_requirement(monkeypatch):
    calls = []
    async def start(*args, **kwargs):
        assert kwargs['start_new_session'] is True
        folder = Path(args[-1])
        calls.append(json.loads((folder/'request.json').read_text()))
        (folder/'result.json').write_text(json.dumps({'status': 'available', 'populationConserved': True, 'roads': []}))
        async def communicate():
            await asyncio.sleep(0)
            return b'', b''
        return NS(returncode=0, communicate=communicate)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', start)
    service = Class2ForecastService()
    runtime = NS(requirement_record={'fingerprint': 'frozen-one'}, random_seed=1, adapter=NS(sumo_binary='sumo'))
    first, second = await asyncio.gather(service.forecast(runtime), service.forecast(runtime))
    assert len(calls) == 1
    first['roads'].append('foreign')
    assert second['roads'] == []
    runtime.requirement_record = {'fingerprint': 'other-conversation'}
    await service.forecast(runtime)
    assert len(calls) == 2 and service.process is None


@async_test
async def test_worker_timeout_terminates_its_process_group_and_does_not_cache(monkeypatch):
    kills = []
    async def communicate():
        raise asyncio.TimeoutError
    proc = NS(returncode=None, pid=123, communicate=communicate)
    async def wait():
        proc.returncode = -15
    proc.wait = wait
    async def start(*args, **kwargs):
        return proc
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', start)
    monkeypatch.setattr('os.killpg', lambda *args: kills.append(args))
    service = Class2ForecastService()
    result = await service.forecast(NS(requirement_record={'fingerprint': 'one'}, random_seed=1, adapter=NS(sumo_binary='sumo')))
    assert result['status'] == 'unavailable' and kills[0][0] == 123
    assert service.process is None and service.cached is None


@async_test
async def test_http_rejects_stale_sources_and_does_not_preview_paused_or_first_class():
    source = {'runId': 'run-one', 'frameId': 'frame-one'}
    runtime = fixture()
    runtime.class2_flows.control = NS(binding=lambda r: source.copy())
    async def forecast(r):
        r.state = RuntimeState.PAUSED
        return {'status': 'available'}
    server = NS(simulator=runtime, _class2_forecast_service=NS(forecast=forecast), _error=OverlayServer._error)
    async def body():
        return source.copy()
    request = NS(json=body)
    response = await OverlayServer._class2_baseline_preview(server, request)
    assert response.status == 409
    response = await OverlayServer._class2_baseline_preview(server, request)
    assert json.loads(response.body)['baseline']['status'] == 'unavailable'
    server.simulator = NS(class2_flows=None)
    assert (await OverlayServer._class2_baseline_preview(server, request)).status == 409
