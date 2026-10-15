"""Stage-1 contract and lifecycle tests; all simulation/network boundaries are fakes."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from crowdsim.core.during_observation import DuringObservation
from crowdsim.core.simulation_runtime import SimulationRuntime, RuntimeState
from crowdsim.domain.during_preset import load_during_preset, preset_fingerprint, verify_bound_preset
from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.experiments.contracts import ContractError, validate_experiment_config
from crowdsim.experiments.controller_registry import ControllerRegistry
from crowdsim.experiments.metrics import summarize_observations, summarize_during_runs
from crowdsim.experiments.observation_builder import ObservationBuilder
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.report_service import ReportService
from crowdsim.infrastructure.frame_serializer import FrameSerializer
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.websocket_server import OverlayServer

PRESET_ID = 'memorial-tower-during-v1'


@pytest.fixture(autouse=True)
def no_live_simulation_or_network(monkeypatch):
    import socket
    import subprocess
    from crowdsim.infrastructure.sumo_adapter import SumoAdapter
    def forbidden(*args, **kwargs):
        raise AssertionError('Stage-1 unit tests must not launch SUMO, processes or network operations')
    monkeypatch.setattr(SumoAdapter, 'start', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket.socket, 'bind', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


def config(**scenario):
    return validate_experiment_config({'name': 'during baseline', 'scenario': {'presetId': PRESET_ID, **scenario}})


def test_preset_resolves_full_requirement_and_fixed_baseline():
    data = config()
    assert data['methods'] == ['C0']
    scenario = data['scenario']
    assert scenario['population'] == 700
    assert scenario['durationSeconds'] == 1800
    assert scenario['seedSet'] == list(range(20260908, 20260913))
    assert verify_bound_preset(scenario)['requirement']['population']['total'] == 700
    scenario['duringPreset']['phenomenon']['minimum_count'] = 9
    with pytest.raises(ValueError, match='snapshot mismatch'):
        verify_bound_preset(scenario)


@pytest.mark.parametrize('values', [
    {'population': 701}, {'durationSeconds': 600}, {'locationId': 'east-nanjing-road'},
    {'seedSet': []}, {'seedSet': [1, 1]}, {'seedSet': [True]}, {'seedSet': [-1]},
    {'presetVersion': 2}, {'emergencyEvent': {'enabled': True}}, {'presetId': '../../x'},
    {'customFormula': 'x'}, {'wallTimeoutSeconds': float('nan')}, {'speedFactor': 0},
])
def test_unsupported_preset_customization_is_rejected(values):
    with pytest.raises(ContractError):
        config(**values)


def test_stage1_rejects_live_control_methods():
    with pytest.raises(ContractError, match='only the C0'):
        validate_experiment_config({'name': 'bad', 'scenario': {'presetId': PRESET_ID}, 'methods': ['C2']})


def test_stale_preset_is_rejected_before_queueing(monkeypatch):
    orchestrator = ExperimentOrchestrator(CrowdSimRepository(), Gateway(), ControllerRegistry())
    experiment = orchestrator.create_experiment('u', 'w', {'name': 'baseline', 'scenario': {'presetId': PRESET_ID}})
    experiment['scenario']['presetFingerprint'] = 'changed'
    orchestrator.repository.update_experiment('u', 'w', experiment['experiment_id'], {'scenario': experiment['scenario']})
    with pytest.raises(ValueError, match='changed since'):
        orchestrator.start_experiment('u', 'w', experiment['experiment_id'])
    assert orchestrator._queue.empty()


def test_concurrent_starts_create_and_queue_one_complete_seed_matrix(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import time

    repository = CrowdSimRepository()
    orchestrator = ExperimentOrchestrator(repository, Gateway(), ControllerRegistry())
    monkeypatch.setattr(orchestrator, '_ensure_worker', lambda: None)
    monkeypatch.setattr('crowdsim.experiments.orchestrator.runtime_provenance', lambda: {})
    experiment = orchestrator.create_experiment('u', 'w', {'name': 'concurrent', 'scenario': {'presetId': PRESET_ID}})
    original_list = repository.list_runs
    def delayed_list(*args):
        rows = original_list(*args)
        time.sleep(.03)  # Expose the check/create race without relying on disk speed.
        return rows
    monkeypatch.setattr(repository, 'list_runs', delayed_list)
    barrier = threading.Barrier(8)
    def start(_):
        barrier.wait(timeout=5)
        return orchestrator.start_experiment('u', 'w', experiment['experiment_id'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(start, range(8)))
    runs = original_list('u', 'w', experiment['experiment_id'])
    assert len(runs) == orchestrator._queue.qsize() == 5
    expected_ids = {run['run_id'] for run in runs}
    assert all({run['run_id'] for run in rows} == expected_ids for rows in results)
    assert {run['seed'] for run in runs} == set(range(20260908, 20260913))


def record(preset):
    spec = RequirementSpec.parse(preset['requirement'])
    return {'requirement_id': 'req-test', 'requirement': spec.payload, 'fingerprint': 'sha256:' + spec.fingerprint}


def test_runtime_factory_sets_population_duration_and_all_seeds_without_starting_sumo():
    preset = load_during_preset(PRESET_ID)
    runtime = SimulationRuntime.for_during_preset(preset, record(preset), 17)
    assert runtime.state == RuntimeState.CREATED
    assert runtime.demand_count == 700 and runtime.timeline_end_seconds == 1800
    assert runtime.hotspot_demand_spec.seed == runtime.population.profile_sampler.seed == runtime.random_seed == 17
    assert list(runtime.adapter.extra_args) == ['--seed', '17', '--random=false']
    assert runtime.use_llm is False and runtime.during_observer is not None
    with pytest.raises(ValueError, match='requirement does not match'):
        bad = record(preset)
        bad['requirement']['population']['total'] = 800
        SimulationRuntime.for_during_preset(preset, bad, 17)


def test_reset_replaces_sumo_seed_and_clears_event_history(monkeypatch):
    preset = load_during_preset(PRESET_ID)
    runtime = SimulationRuntime.for_during_preset(preset, record(preset), 17)
    previous = runtime.during_observer
    previous.t95 = 123
    monkeypatch.setattr(runtime, 'close', lambda: None)
    monkeypatch.setattr(SimulationRuntime, 'initialize', lambda self: self.state)
    runtime.reset(700, seed=18)
    assert list(runtime.adapter.extra_args) == ['--seed', '18', '--random=false']
    assert runtime.during_observer is not previous
    assert runtime.during_observer.t95 is None
    assert runtime.during_observer.tracker.snapshot()['occurred'] is None


class Gateway:
    capabilities = {}

    def __init__(self):
        self.commands = []

    def add_listener(self, callback):
        self.callback = callback

    def submit(self, payload):
        self.commands.append(deepcopy(payload))
        return str(len(self.commands))

    def wait_for_ack(self, request_id, timeout):
        requirement = self.commands[int(request_id)-1]['requirement']
        return {'type': 'requirement_accepted', 'requirement_id': 'req-preset',
                'fingerprint': 'sha256:' + RequirementSpec.parse(requirement).fingerprint}


def test_reset_payload_uses_preset_not_current_runtime_requirement():
    gateway = Gateway()
    orchestrator = ExperimentOrchestrator(CrowdSimRepository(), gateway, ControllerRegistry())
    scenario = config()['scenario']
    payload = orchestrator._prepare_reset_payload(scenario, 12)
    assert [item['action'] for item in gateway.commands] == ['submit_experiment_requirement']
    assert gateway.commands[0]['requirement'] == scenario['duringPreset']['requirement']
    assert payload['seed'] == 12 and payload['count'] == 700
    assert payload['during_preset_fingerprint'] == scenario['presetFingerprint']
    assert payload['requirement_id'] == 'req-preset'


def test_websocket_preset_reset_constructs_new_runtime_before_initialization(tmp_path, monkeypatch):
    preset = load_during_preset(PRESET_ID)
    repository = RequirementRepository(tmp_path)
    accepted = repository.create(RequirementSpec.parse(preset['requirement']))
    old_runtime = SimpleNamespace(_sumo_binary=None, performance_options=None, run_id='old')
    server = OverlayServer(old_runtime, 'localhost', 0, requirement_repository=repository)
    server._stop_runtime = AsyncMock()
    server._prepare = AsyncMock()
    server._send_init = AsyncMock()
    payload = {'during_preset_id': PRESET_ID, 'during_preset_version': 1,
               'during_preset_fingerprint': preset_fingerprint(preset),
               'requirement_id': accepted['requirement_id'], 'count': 700}
    asyncio.run(server._configure(payload, 'request', replace_run=True, seed=99))
    assert server.simulator is not old_runtime
    assert server.simulator.requirement_id == accepted['requirement_id']
    assert server.simulator.random_seed == 99 and server.simulator.demand_count == 700
    server._prepare.assert_awaited_once_with('request', 700)
    server._send_init.assert_awaited_once_with('request')


def measured_evidence(*, through=40, finished=False, missing=False):
    preset = load_during_preset(PRESET_ID)
    observer = DuringObservation(preset)
    # 50 core pedestrians, 20 crowded people on an entrance, 630 elsewhere.
    people = {str(i): SimpleNamespace(person_id=str(i), edge_id=(preset['phenomenon']['scope_edges'][i % 3]
              if i < 50 else preset['control_scope']['entry_ids'][0])) for i in range(70)}
    population = {'planned': 700, 'departed': 700, 'active': 700, 'arrived': 0,
                  'explicitly_removed': 0, 'unknown_disappearances': [], 'conservation_error': 0}
    runtime = SimpleNamespace(current=SimpleNamespace(persons=people),
        population=SimpleNamespace(diagnostics=lambda: dict(population)),
        latest_metrics={'hotspots': {preset['hotspot_id']: {'density_person_per_m2': .2}},
                        'queue': {'blocked_low_speed': 3}, 'control_queue': {'inflow_held': 0}})
    for half_tick in range(1, int(through*2)+1):
        runtime.time_seconds = half_tick / 2
        runtime.visual_states = {str(i): {'density_valid': half_tick != 1 and not (missing and half_tick == 40),
            'visual_state': 'crowded' if (i < 10 and runtime.time_seconds <= 12) or i >= 50 else 'planned_stop'} for i in range(70)}
        # Exact crossing at 31.5s must survive a 30-second service sampling interval.
        if runtime.time_seconds >= 31.5:
            population.update(arrived=665, active=35)
        result = observer.measure(runtime, finished=finished and half_tick == int(through*2))
    return result


def observation(during):
    return ObservationBuilder().build({'snapshot_id': 'sim:80', 'step_seconds': during['time_seconds'],
        'metrics': {'during': during, 'pedestrian_count': 0}}, run_id='run')


def test_short_event_between_service_samples_and_t95_are_preserved():
    during = measured_evidence()
    assert during['phenomenon']['first_confirmed_at_seconds'] == 11
    assert during['phenomenon']['episodes'][0]['end_seconds'] == 12.5
    assert during['phenomenon']['peak_crowded_count'] == 10  # Entrance crowd not counted.
    assert during['sample']['core_count'] == 50
    assert during['t95_from_start_seconds'] == 31.5
    summary = summarize_observations([observation(during)])
    assert summary['crowdingOccurred'] is True
    assert summary['crowdingDurationSeconds'] == 11.5
    assert summary['t95Seconds'] == 31.5 and summary['finalCompletionRate'] == .95


def test_final_result_keeps_missing_coverage_and_unreached_t95():
    during = measured_evidence(through=20, finished=True, missing=True)
    summary = summarize_observations([observation(during)])
    assert summary['crowdingCoverage'] == 'incomplete'
    assert summary['t95Seconds'] is None
    assert during['end_reason'] == 'premature_end' and during['run_complete'] is False


def final_evidence():
    during = measured_evidence()
    during.update(time_seconds=1800, finished=True, run_complete=True, end_reason='observation_horizon')
    return during


def active_run():
    repository = CrowdSimRepository()
    gateway = Gateway()
    orchestrator = ExperimentOrchestrator(repository, gateway, ControllerRegistry())
    experiment = orchestrator.create_experiment('u', 'w', {'name': 'x', 'scenario': {'presetId': PRESET_ID}})
    run = repository.create_run('u', 'w', experiment['experiment_id'], 'C0', 1, {})
    controller = ControllerRegistry().create('C0')
    active = {'run': run, 'experiment': experiment, 'user_id': 'u', 'workspace_id': 'w',
              'controller': controller, 'builder': ObservationBuilder(), 'terminal': threading.Event(),
              'runtime_run_id': 'sim', 'last_observation_time': 1799, 'last_decision': None,
              'last_effect': None, 'pending_evaluations': [], 'failure': None}
    return orchestrator, active


def test_terminal_uppercase_frame_is_always_saved_even_between_sampling_intervals():
    orchestrator, active = active_run()
    during = final_evidence()
    during.update(time_seconds=1799.5, end_reason='all_arrived')
    frame = {'run_id': 'sim', 'runtime_state': 'FINISHED', 'step_seconds': 1799.5, 'metrics': {'during': during}}
    orchestrator._handle_frame(active, frame)
    assert active['terminal'].is_set() and active['failure'] is None
    rows = orchestrator.repository.list_records('observations', 'u', 'w', active['run']['run_id'])
    assert len(rows) == 1 and rows[0]['payload']['during']['finished'] is True


@pytest.mark.parametrize('kind', ['missing', 'premature', 'fingerprint'])
def test_preset_run_cannot_succeed_without_final_backend_evidence(kind):
    orchestrator, active = active_run()
    during = final_evidence()
    if kind == 'premature':
        during['run_complete'] = False
    if kind == 'fingerprint':
        during['preset_fingerprint'] = 'wrong'
    orchestrator._handle_frame(active, {'run_id': 'sim', 'runtime_state': 'FINISHED',
        'step_seconds': 1800, 'metrics': {'during': None if kind == 'missing' else during}})
    assert active['terminal'].is_set() and active['failure']


def test_previous_runtime_frame_cannot_terminate_new_round():
    orchestrator, active = active_run()
    orchestrator._handle_frame(active, {'run_id': 'old', 'runtime_state': 'FINISHED', 'step_seconds': 1800})
    assert not active['terminal'].is_set()


@pytest.mark.parametrize('accept_frames', [True, False])
def test_runtime_error_terminates_only_the_current_run_even_before_first_frame(accept_frames):
    orchestrator, active = active_run()
    orchestrator._active = active
    active['accept_frames'] = accept_frames
    event = {'type': 'runtime_error', 'run_id': 'old', 'message': 'No space left on device'}
    orchestrator._on_gateway_event(event)
    assert not active['terminal'].is_set()
    event['run_id'] = 'sim'
    orchestrator._on_gateway_event(event)
    assert active['terminal'].is_set()
    assert active['failure'] == event['message']


def test_report_denominator_excludes_failed_cancelled_and_incomplete_rounds():
    scenario = config()['scenario']
    good = final_evidence()
    negative = deepcopy(good)
    negative['phenomenon']['occurred'] = False
    incomplete = deepcopy(good)
    incomplete['phenomenon']['coverage'] = 'incomplete'
    rows = [{'status': 'completed', 'during': good}, {'status': 'completed', 'during': negative},
            {'status': 'completed', 'during': incomplete}, {'status': 'failed', 'during': good}, {'status': 'cancelled'}]
    result = summarize_during_runs(rows, scenario)
    assert result['validRunCount'] == 2 and result['occurrenceFrequency'] == .5
    assert result['incompleteRunCount'] == result['failedRunCount'] == result['cancelledRunCount'] == 1
    assert summarize_during_runs([], scenario)['occurrenceFrequency'] is None


def test_report_retains_evidence_after_repository_reload(tmp_path):
    repository = CrowdSimRepository(tmp_path)
    experiment = repository.create_experiment('u', 'w', config(seedSet=[1]))
    run = repository.create_run('u', 'w', experiment['experiment_id'], 'C0', 1, {})
    repository.record('observations', 'u', 'w', run['run_id'], observation(final_evidence()))
    repository.update_run('u', 'w', run['run_id'], {'status': 'completed'})
    report = ReportService(CrowdSimRepository(tmp_path)).generate('u', 'w', experiment['experiment_id'])
    assert report['during']['occurrenceFrequency'] == 1
    assert report['runs'][0]['t95Seconds'] == 31.5
    assert report['pairedComparisons'] == []
    assert report['dataScope']['descriptiveOnly'] is True


def test_initial_missing_formal_tick_is_not_reported_as_no_event():
    from crowdsim.domain.during_crowding import CrowdingTracker
    tracker = CrowdingTracker(load_during_preset(PRESET_ID)['phenomenon'])
    tracker.update({'time_seconds': 1.5, 'core_count': 0, 'crowded_count': 0, 'high_risk_count': 0, 'unknown_count': 0})
    assert tracker.snapshot()['coverage'] == 'incomplete'
    assert tracker.snapshot()['occurred'] is None


@pytest.mark.parametrize('args', [
    ['--seed', '10'], ['--seed=10', '--random'], ['-s', '10', '--random', 'true'],
    ['--random=false'],
])
def test_explicit_round_seed_overrides_seed_and_random_flags(args):
    preset = load_during_preset(PRESET_ID)
    runtime = SimulationRuntime(Path(__file__).resolve().parents[1] / preset['sumo_config'],
                                extra_sumo_args=args + ['--no-warnings'], random_seed=23)
    assert runtime.adapter.extra_args == ['--no-warnings', '--seed', '23', '--random=false']


def test_frame_carries_backend_evidence_without_recounting_rendered_pedestrians():
    preset = load_during_preset(PRESET_ID)
    runtime = SimulationRuntime.for_during_preset(preset, record(preset), 1)
    runtime.latest_metrics['during'] = final_evidence()
    payload = FrameSerializer()._metrics(runtime, pedestrians=[])
    assert payload['during']['sample']['core_count'] == 50
    payload['during']['sample']['core_count'] = 0
    assert runtime.latest_metrics['during']['sample']['core_count'] == 50


def test_runtime_recorder_archives_preset_per_tick_evidence_and_final_result(tmp_path, monkeypatch):
    from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
    monkeypatch.setattr('crowdsim.infrastructure.experiment_recorder.subprocess.run',
                        lambda *args, **kwargs: SimpleNamespace(stdout='test-commit'))
    preset = load_during_preset(PRESET_ID)
    config_path = Path(__file__).resolve().parents[1] / preset['sumo_config']
    recorder = ExperimentRecorder('unit-run', config_path, {}, root=tmp_path)
    try:
        recorder.attach_during_preset(preset)
        during = final_evidence()
        metrics = {'pedestrian_count': 0, 'vehicle_count': 0, 'pedestrian_avg_speed_mps': 0,
                   'vehicle_avg_speed_mps': 0, 'population': {'conservation_error': 0}, 'during': during}
        result = SimpleNamespace(time_seconds=1800, persons={}, departed_person_ids=[], arrived_person_ids=[])
        recorder.record_step(SimpleNamespace(), result, metrics)
        saved = json.loads((recorder.directory / 'during_result.json').read_text())
        assert saved == during
        samples = (recorder.directory / 'during_observations.jsonl').read_text().splitlines()
        assert len(samples) == 1 and json.loads(samples[0]) == during
        assert json.loads((recorder.directory / 'during_preset.json').read_text()) == preset
        assert json.loads((recorder.directory / 'manifest.json').read_text())['during']['preset_fingerprint'] == preset_fingerprint(preset)
    finally:
        recorder.abort('unit test cleanup')


def test_execute_run_completes_through_fake_gateway_and_records_actual_runtime_id():
    class Terminal:
        flag = False
        def is_set(self):
            return self.flag
        def set(self):
            self.flag = True
        def wait(self, timeout):
            assert orchestrator._active['accept_frames'] is True
            gateway.callback({'type': 'update', 'run_id': 'runtime-fake', 'runtime_state': 'FINISHED',
                              'step_seconds': 1800, 'metrics': {'during': final_evidence()}})
            return self.flag

    class ScriptedGateway(Gateway):
        def start(self, wait_seconds):
            return True
        def wait_for_ack(self, request_id, timeout):
            action = self.commands[int(request_id)-1]['action']
            if action == 'submit_experiment_requirement':
                return super().wait_for_ack(request_id, timeout)
            if action == 'reset':
                return {'type': 'init', 'run_id': 'runtime-fake',
                        'requirement': {'requirement_id': 'req-fake', 'population_total': 700},
                        'metrics': {'during': measured_evidence(through=1)}}
            if action == 'start':
                orchestrator._active['terminal'] = Terminal()
            return {'type': 'command_result', 'status': 'applied'}

    gateway = ScriptedGateway()
    repository = CrowdSimRepository()
    orchestrator = ExperimentOrchestrator(repository, gateway, ControllerRegistry())
    experiment = orchestrator.create_experiment('u', 'w', {'name': 'x', 'scenario': {'presetId': PRESET_ID, 'seedSet': [9]}})
    run = repository.create_run('u', 'w', experiment['experiment_id'], 'C0', 9, {})
    orchestrator._execute_run({'user_id': 'u', 'workspace_id': 'w', 'experiment': experiment, 'run': run})
    saved = repository.get_run('u', 'w', run['run_id'])
    assert saved['status'] == 'completed'
    assert saved['runtime_run_id'] == 'runtime-fake' and saved['requirement_id'] == 'req-fake'
    assert saved['summary']['crowdingOccurred'] is True and saved['summary']['t95Seconds'] == 31.5
    assert [command['action'] for command in gateway.commands] == ['submit_experiment_requirement', 'reset', 'set_speed', 'start']
