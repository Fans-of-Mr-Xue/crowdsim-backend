"""Offline comparison scheduling. No listeners, WebSockets, models or SUMO."""
from copy import deepcopy
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from crowdsim.experiments.class2_comparison import Class2ComparisonService, ComparisonConflict
from crowdsim.experiments.repository import CrowdSimRepository
from dialog_storage.during_requirements import DuringRequirementRepository
from dialog_storage.during_workflow import experiment_group_id
from tests.class2_flow_fixtures import flow_request


CID, SID = 'flow-test', 'snapshot-test'
ORIGINAL = 'run-' + 'a' * 32


class FakeGateway:
    def __init__(self):
        self.commands, self.receipts, self.preparations, self.listeners = [], {}, [], []
        self.run_id, self.time, self.state, self.plan = None, .5, 'CLOSED', None
        self.sequence, self.closed, self.pause_at = 0, 0, None
        self.on_step = None
        self.fail_step = False

    def add_listener(self, callback):
        self.listeners.append(callback)

    def remove_listener(self, callback):
        self.listeners.remove(callback)

    def emit(self, payload):
        for callback in self.listeners[:]:
            callback(deepcopy(payload))

    def prepare(self, request):
        self.preparations.append(deepcopy(request))
        return {'requirement_id': 'req-' + str(request['seed']), 'type': 'requirement_accepted',
                'class2_artificial_society': {'model_id': 'asm-test', 'source': request['source']}}

    def start(self, **_):
        return True

    def frame(self):
        return {'type': 'update', 'run_id': self.run_id, 'runtime_state': self.state,
                'pedestrians': [{'id': 'person', 'x': self.time, 'y': 1}], 'vehicles': [],
                'step_seconds': self.time, 'class2_control_context': {'source': {
                    'conversationId': CID, 'snapshotId': SID, 'runId': self.run_id, 'timeSeconds': self.time,
                    'frameId': 'frame-' + str(self.time), 'requirementId': 'req-' + str(self.preparations[-1]['seed'])}},
                'class2_control': {'actions': [{'status': 'completed'}]} if self.plan else None,
                'metrics': {'observations': {'global': {'density_person_per_m2': 2, 'avg_speed_mps': 1.2},
                                           'cells': [], 'boundaries': []}}}

    def pump(self):
        assert self.state == 'RUNNING'
        if self.fail_step:
            self.state = 'ERROR'
            self.emit({'type': 'runtime_error', 'run_id': self.run_id, 'message': 'fake interrupted transport'})
            return
        self.time += .5
        self.state = 'FINISHED' if self.time >= 10 else 'PAUSED' if self.pause_at and self.time >= self.pause_at else 'RUNNING'
        self.emit(self.frame())
        if self.on_step:
            self.on_step({'targetTimeSeconds': self.time})

    def submit(self, payload):
        self.commands.append(deepcopy(payload)); self.sequence += 1
        action = payload['action']
        if action == 'get_status':
            result = {'type': 'status', 'state': self.state, 'run_id': self.run_id,
                      'scenario': {'class2_demo': {'source': {'conversationId': CID, 'snapshotId': SID}}}}
        elif action == 'configure':
            assert self.plan is None, 'previous decision leaked into initialization'
            assert self.state in {'CLOSED', 'CREATED', 'ERROR'}
            self.run_id = 'run-' + f'{self.sequence:032x}'
            self.time, self.state = .5, 'READY'
            request = self.preparations[-1]
            result = {'type': 'init', 'run_id': self.run_id, 'step_seconds': self.time,
                      'step_length': .5, 'real_step_interval': .5 / 3,
                      'requirement': {'requirement_id': payload['requirement_id']},
                      'scenario': {'network_sha256': request['flowDesign']['network_sha256'],
                                   'class2_artificial_society': {'model_id': 'asm-test', 'source': request['source']},
                                   'class2_demo': {'source': request['source'], 'seed': request['seed'], 'flow_design': request['flowDesign']}}}
        elif action == 'get_state':
            result = self.frame(); self.emit(result)
        elif action in {'start', 'pause', 'set_speed'}:
            if action == 'start':
                if self.state not in {'READY', 'PAUSED'}:
                    identity = 'ack-' + str(self.sequence)
                    self.receipts[identity] = {'type': 'error', 'run_id': self.run_id,
                                               'message': 'start is invalid in state ' + self.state}
                    return identity
                self.pause_at = payload.get('pauseAtSeconds'); self.state = 'RUNNING'
            elif action == 'pause':
                self.state = 'PAUSED'; self.emit(self.frame())
            result = {'type': 'speed' if action == 'set_speed' else 'command_result', 'run_id': self.run_id,
                      'action': action, 'status': 'applied', 'speedFactor': payload.get('speedFactor')}
        elif action == 'install_class2_control_plan':
            from crowdsim.core.runtime_commands import RuntimeCommand, RuntimeCommandQueue
            assert self.plan is None and self.state in {'READY', 'PAUSED'}, 'multiple decisions installed in one run'
            self.plan = deepcopy(payload['controlPlan'])
            queue = RuntimeCommandQueue()
            queue.submit(RuntimeCommand(action, payload, str(self.sequence), self.time, 'frame'))
            result = queue.apply_all(lambda *_: deepcopy(self.plan), applied_at=self.time, snapshot_id='frame')[0]
        elif action == 'close_run':
            result = {'type': 'run_closed', 'run_id': self.run_id}
            self.plan, self.state = None, 'CLOSED'
        else:
            raise AssertionError(action)
        identity = 'ack-' + str(self.sequence)
        self.receipts[identity] = result
        return identity

    def wait_for_ack(self, identity, **_):
        return deepcopy(self.receipts[identity])

    def forget_request(self, _):
        pass

    def close(self):
        self.closed += 1


@pytest.fixture
def setup(tmp_path, monkeypatch):
    request = flow_request(durationSeconds=10)
    r = {'schema_version': 2, 'project': {'title': request['projectTitle'], 'description': request['phenomenon']},
         'research': {'problem_type': 2}, 'spatial_scope': {'name': request['requestedLocation']},
         'population': {'total': request['count'], 'distributions': request['populationDistributions']},
         'experiment': {'duration_seconds': 10}, 'observation': {'metric_ids': request['metricIds']},
         'class2': {'flow_design': request['flowDesign']}}
    snapshot = {'id': SID, 'requirement': deepcopy(r), 'modeling_requirements': {'revision': 1}}
    requirements = DuringRequirementRepository(tmp_path / 'requirements')
    requirements.save(CID, state={'requirement': {'version': 3, 'draft': {'id': 'draft', 'revision': 1, 'requirement': r},
                                                  'snapshots': [snapshot], 'submissions': {}}}, expected_revision=0, mutation_id='confirm')
    actions = [{'actionId': 'one', 'actionType': 'observe_only', 'startSeconds': 3, 'target': {}, 'parameters': {}}]
    scene = {'snapshotId': SID, 'runId': ORIGINAL, 'runtimeState': 'PAUSED', 'capturedAt': 'captured',
             'networkSha256': request['flowDesign']['network_sha256'],
             'controlContext': {'source': {'conversationId': CID, 'snapshotId': SID, 'runId': ORIGINAL, 'timeSeconds': 2.5}}}
    generation = {'sceneIdentity': json.dumps([SID, ORIGINAL, 'req-old', 'captured', 2.5, 'PAUSED', scene['networkSha256']]),
                  'options': [{'id': 'a', 'label': '决策甲', 'text': '甲方案', 'actions': actions},
                              {'id': 'b', 'label': '决策乙', 'text': '乙方案', 'actions': [{**actions[0], 'actionId': 'two'}]}]}
    archive = {'artifacts': {'generation': {'id': 'generation', 'snapshotId': SID, 'kind': 'generation', 'revision': 1, 'payload': generation},
                             'scene': {'id': 'scene', 'snapshotId': SID, 'runId': ORIGINAL, 'kind': 'scene', 'payload': scene}}}
    artifacts = SimpleNamespace(load=lambda *_: deepcopy(archive))
    gateway = FakeGateway(); repo = CrowdSimRepository(tmp_path / 'control')
    service = Class2ComparisonService(tmp_path / 'comparisons', repo, gateway, requirements, artifacts, gateway.prepare, auto_start=False)
    # Advance a fake engine only when the queue waits for a normal update.
    monkeypatch.setattr(service._condition, 'wait', lambda **_: gateway.pump())
    body = {'snapshotId': SID, 'generationId': 'generation', 'generationRevision': 1, 'candidateIds': ['a', 'b'],
            'repetitions': 2, 'clientRequestId': 'submission', 'expectedVersion': 0}
    return service, gateway, repo, body, archive


def start(service, body):
    configured = service.configure('user', 'during-' + CID, CID, body)
    return service.start('user', 'during-' + CID, CID, {'executionVersion': configured['executionVersion']})


def test_confirming_configuration_waits_for_explicit_start_and_survives_restart(setup):
    service, gateway, repo, body, _ = setup
    configured = service.configure('user', 'during-' + CID, CID, body)
    assert configured['status'] == 'ready' and len(configured['runs']) == 4
    assert service._active is None and not gateway.commands and not gateway.preparations
    assert not repo._memory['leases']
    reopened = Class2ComparisonService(service.root, repo, gateway, service.requirements, service.artifacts, gateway.prepare, auto_start=False)
    assert reopened.load('user', 'during-' + CID, CID)['status'] == 'ready'
    assert reopened.runtime('user', 'during-' + CID, CID)['init'] is None
    with pytest.raises(ComparisonConflict):
        reopened.start('user', 'during-' + CID, CID, {'executionVersion': 0})
    queued = reopened.start('user', 'during-' + CID, CID, {'executionVersion': 1})
    assert queued['status'] == 'queued' and reopened._active == CID
    assert reopened.start('user', 'during-' + CID, CID, {'executionVersion': 1}) == queued
    assert not gateway.commands


def test_matrix_is_frozen_scoped_and_idempotent(setup):
    service, gateway, _, body, archive = setup
    record = start(service, body)
    assert record['experimentId'] == experiment_group_id(CID)
    assert [(r['candidateId'], r['round'], r['seed']) for r in record['runs']] == [
        ('a', 1, 20260908), ('a', 2, 20260909), ('b', 1, 20260908), ('b', 2, 20260909)]
    assert start(service, body) == record
    archive['artifacts']['generation']['payload']['options'][0]['text'] = 'later edit'
    assert service.load('user', 'during-' + CID, CID)['candidates'][0]['text'] == '甲方案'
    assert not gateway.commands
    with pytest.raises(ComparisonConflict):
        start(service, {**body, 'repetitions': 3})
    with pytest.raises(ValueError):
        service.load('user', 'during-other', CID)
    with pytest.raises(ValueError):
        service.load('other-user', 'during-' + CID, CID)


@pytest.mark.parametrize('changes', [{'candidateIds': []}, {'candidateIds': ['a', 'a']}, {'candidateIds': ['foreign']},
                                     {'repetitions': True}, {'repetitions': 0}, {'repetitions': 31}, {'snapshotId': 'foreign'},
                                     {'generationRevision': 2}, {'generationId': 'missing'}])
def test_rejects_invalid_or_foreign_configuration_before_engine_access(setup, changes):
    service, gateway, _, body, _ = setup
    with pytest.raises(ValueError):
        start(service, {**body, **changes})
    assert not gateway.commands


@pytest.mark.parametrize('candidate_ids', [['a', 'b'], ['b', 'a']])
def test_completes_each_decision_before_the_next_with_fresh_initialization_and_paired_seeds(setup, candidate_ids):
    service, gateway, _, body, _ = setup
    start(service, {**body, 'candidateIds': candidate_ids}); service.execute(CID)
    result = service.load('user', 'during-' + CID, CID)
    assert result['status'] == 'completed'
    assert [r['status'] for r in result['runs']] == ['completed'] * 4
    assert [r['seed'] for r in gateway.preparations] == [20260908, 20260909, 20260908, 20260909]
    actions = [c['action'] for c in gateway.commands]
    assert actions.count('configure') == actions.count('install_class2_control_plan') == actions.count('close_run') == 4
    installed = [c['controlPlan']['actions'][0]['actionId'] for c in gateway.commands if c['action'] == 'install_class2_control_plan']
    assert installed == [{'a': 'one', 'b': 'two'}[candidate] for candidate in candidate_ids for _ in range(2)]
    assert all(r['installReceipt']['status'] == 'applied' and r['runtimeRunId'] for r in result['runs'])
    assert all(r['firstSampleTime'] > 2.5 and r['lastObservation']['timeSeconds'] == 10 for r in result['runs'])
    assert all((service.root / r['sampleArchive']).is_file() for r in result['runs'])
    assert [s['completedRuns'] for s in result['summary']] == [2, 2]
    assert result['summary'][0]['metrics']['global-speed']['mean'] == 1.2
    assert gateway.closed == 1
    assert not service.repository._memory['leases']
    runtime = service.runtime('user', 'during-' + CID, CID)
    assert runtime['record']['status'] == 'completed'
    assert runtime['runId'] == result['runs'][-1]['id']
    assert runtime['frame']['run_id'] == runtime['init']['run_id'] == result['runs'][-1]['runtimeRunId']
    assert runtime['frame']['pedestrians'][0]['x'] == 10
    reopened = Class2ComparisonService(service.root, service.repository, gateway, service.requirements, service.artifacts, gateway.prepare, auto_start=False)
    restored = reopened.runtime('user', 'during-' + CID, CID)
    assert restored['record'] == runtime['record'] and restored['frame'] is None and restored['init'] is None
    assert not (service.root / CID / 'runtime.json').exists()


def test_normal_engine_stream_updates_map_and_samples_independently_of_viewers(setup):
    service, gateway, _, body, _ = setup
    observed = []
    def receive(payload):
        if payload.get('type') == 'update':
            observed.append(payload['step_seconds'])
    gateway.add_listener(receive)
    start(service, {**body, 'candidateIds': ['a'], 'repetitions': 1})
    service.execute(CID)
    result = service.load('user', 'during-' + CID, CID)
    assert result['status'] == 'completed'
    assert set(observed) == {i * .5 for i in range(1, 21)}
    assert result['runs'][0]['sampleCount'] == 15
    for _ in range(3):
        service.runtime('user', 'during-' + CID, CID)
        service.events.after(0, run_id=CID)
    assert service.load('user', 'during-' + CID, CID)['runs'][0]['sampleCount'] == 15
    assert not any('class2_batch' in c['action'] for c in gateway.commands)
    starts = [c for c in gateway.commands if c['action'] == 'start']
    assert len(starts) == 2 and starts[0]['pauseAtSeconds'] == 2.5 and 'pauseAtSeconds' not in starts[1]
    install = next(c for c in gateway.commands if c['action'] == 'install_class2_control_plan')
    assert install['controlPlan']['source']['timeSeconds'] == 2.5
    assert not (service.root / CID / 'runtime.json').exists()
    events = service.events.after(0, run_id=CID)
    frames = [e['payload'] for e in events if e['type'] == 'frame']
    assert frames and 'record' not in frames[-1] and 'init' not in frames[-1]


@pytest.mark.parametrize('phase', ['warmup', 'after_install', 'initialized_scene'])
def test_start_ack_does_not_restart_a_running_engine_when_an_older_frame_arrives(setup, monkeypatch, phase):
    service, gateway, _, body, archive = setup
    if phase == 'initialized_scene':
        archive['artifacts']['scene']['payload']['controlContext']['source']['timeSeconds'] = .5
        generation = archive['artifacts']['generation']['payload']
        identity = json.loads(generation['sceneIdentity']); identity[4] = .5
        generation['sceneIdentity'] = json.dumps(identity)
    submit = gateway.submit
    delayed = []
    def deliver_late_frame(payload):
        request = submit(payload)
        selected = 'warmup' if 'pauseAtSeconds' in payload else 'after_install'
        if payload['action'] == 'start' and selected == ('after_install' if phase == 'initialized_scene' else phase) and not delayed:
            old = gateway.frame()
            old['runtime_state'] = 'READY' if gateway.time == .5 else 'PAUSED'
            gateway.emit(old)
            delayed.append(old)
        return request
    monkeypatch.setattr(gateway, 'submit', deliver_late_frame)
    start(service, {**body, 'candidateIds': ['a'], 'repetitions': 1})
    service.execute(CID)
    result = service.load('user', 'during-' + CID, CID)
    assert result['status'] == 'completed', result.get('error')
    assert len(delayed) == 1
    assert len([c for c in gateway.commands if c['action'] == 'start']) == (1 if phase == 'initialized_scene' else 2)
    assert result['runs'][0]['sampleCount'] == (19 if phase == 'initialized_scene' else 15)


def test_waiting_for_first_running_frame_does_not_repeat_start_after_a_spurious_wakeup(setup, monkeypatch):
    service, gateway, _, body, _ = setup
    wakeups = []
    def wait(**_):
        if not wakeups and gateway.state == 'RUNNING':
            wakeups.append(True)
            return
        gateway.pump()
    monkeypatch.setattr(service._condition, 'wait', wait)
    start(service, {**body, 'candidateIds': ['a'], 'repetitions': 1})
    service.execute(CID)
    result = service.load('user', 'during-' + CID, CID)
    assert result['status'] == 'completed', result.get('error')
    assert len([c for c in gateway.commands if c['action'] == 'start']) == 2


def test_completed_configuration_can_run_again_without_overwriting_prior_results(setup):
    service, gateway, _, body, _ = setup
    start(service, body); service.execute(CID)
    old = service.load('user', 'during-' + CID, CID)
    new = service.start('user', 'during-' + CID, CID, {'executionVersion': 1})
    assert new['executionVersion'] == 2 and new['status'] == 'queued'
    assert [(r['candidateId'], r['round']) for r in new['runs']] == [('a', 1), ('a', 2), ('b', 1), ('b', 2)]
    assert not {r['id'] for r in old['runs']} & {r['id'] for r in new['runs']}
    assert service.start('user', 'during-' + CID, CID, {'executionVersion': 1}) == new
    assert service._read(CID)['previous'][-1]['runs'] == [{**r, 'attempts': []} for r in old['runs']]
    service.execute(CID)
    assert service.load('user', 'during-' + CID, CID)['status'] == 'completed'
    assert len(gateway.preparations) == 8


def test_restart_keeps_records_without_starting_an_engine(setup):
    service, gateway, repo, body, _ = setup
    start(service, body)
    reopened = Class2ComparisonService(service.root, repo, gateway, service.requirements, service.artifacts, gateway.prepare, auto_start=False)
    assert reopened.load('user', 'during-' + CID, CID)['status'] == 'interrupted'
    assert not gateway.commands
    assert reopened._active is None


def test_runtime_frames_are_scoped_and_do_not_restore_disk_pictures(setup):
    service, gateway, repo, body, _ = setup
    start(service, body); service.execute(CID)
    with pytest.raises(ValueError):
        service.runtime('other-user', 'during-' + CID, CID)
    with pytest.raises(ValueError):
        service.runtime('user', 'during-other', CID)
    service.configure('user', 'during-' + CID, CID, {**body, 'expectedVersion': 1, 'clientRequestId': 'new'})
    current = service.runtime('user', 'during-' + CID, CID)
    assert current['record']['status'] == 'ready' and current['executionVersion'] == 2
    assert current['init'] is None and current['frame'] is None
    assert not list(service.root.rglob('runtime.json'))


def test_interruption_retry_preserves_completed_rounds_and_retries_only_unfinished(setup):
    service, gateway, _, body, _ = setup
    def interrupt(payload):
        if len(gateway.preparations) == 2:
            gateway.fail_step = True
    gateway.on_step = interrupt
    start(service, body); service.execute(CID)
    old = service.load('user', 'during-' + CID, CID)
    assert old['status'] == 'failed' and old['runs'][0]['status'] == 'completed'
    gateway.on_step = None; gateway.fail_step = False
    service.control('user', 'during-' + CID, CID, 'retry', {'executionVersion': 1})
    assert not service.load('user', 'during-' + CID, CID).get('error')
    service.execute(CID)
    result = service.load('user', 'during-' + CID, CID)
    assert result['status'] == 'completed'
    assert result['runs'][0] == old['runs'][0]
    assert [r['attempt'] for r in result['runs']] == [1, 2, 1, 1]


def test_pause_and_resume_wait_between_exact_steps(setup):
    service, gateway, _, body, _ = setup
    paused = []
    def pause_once(payload):
        if payload.get('targetTimeSeconds') and not paused:
            paused.append(True)
            service.control('user', 'during-' + CID, CID, 'pause', {'executionVersion': 1})
    gateway.on_step = pause_once
    def resume(**_):
        if service.load('user', 'during-' + CID, CID)['status'] == 'paused':
            service.control('user', 'during-' + CID, CID, 'resume', {'executionVersion': 1})
        else:
            gateway.pump()
    start(service, body)
    with patch.object(service._condition, 'wait', side_effect=resume):
        service.execute(CID)
    assert service.load('user', 'during-' + CID, CID)['status'] == 'completed'


def test_speed_control_uses_the_original_command_and_applies_to_subsequent_rounds(setup):
    service, gateway, _, body, _ = setup
    changed = []
    def set_speed(_):
        if not changed:
            changed.append(True)
            service.control('user', 'during-' + CID, CID, 'set_speed', {'executionVersion': 1, 'speedFactor': .5})
    gateway.on_step = set_speed
    start(service, body); service.execute(CID)
    assert service.load('user', 'during-' + CID, CID)['speedFactor'] == .5
    speed = next(c for c in gateway.commands if c['action'] == 'set_speed')
    assert speed['speedFactor'] == .5 and speed['run_id']
    configs = [c for c in gateway.commands if c['action'] == 'configure']
    assert [c['speedFactor'] for c in configs] == [3, .5, .5, .5]


def test_stream_is_read_only_and_does_not_retain_complete_frames_on_disk(setup):
    import io
    from crowdsim.experiments.serve import ControlRequestHandler
    service, gateway, _, body, _ = setup
    service.configure('user', 'during-' + CID, CID, body)
    handler = ControlRequestHandler.__new__(ControlRequestHandler)
    handler.server = SimpleNamespace(bundle=SimpleNamespace(class2_comparisons=service))
    handler.send_response = lambda *_: None
    handler.send_header = lambda *_: None
    handler._cors_headers = lambda: None
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()
    service.events.publish('state', {**service.load('user', 'during-' + CID, CID), 'status': 'completed'}, run_id=CID)
    with patch.object(service.events, 'wait_after', return_value=service.events.after(0, run_id=CID)):
        handler._stream_class2('user', 'during-' + CID, CID)
    assert b'"snapshot"' in handler.wfile.getvalue()
    assert not gateway.commands and service._active is None
    assert not list(service.root.rglob('runtime.json'))
    with pytest.raises(ValueError):
        handler._stream_class2('other', 'during-' + CID, CID)
    service.start('user', 'during-' + CID, CID, {'executionVersion': 1})
    def updates(*_, **__):
        service.events.publish('frame', {'conversationId': CID, 'frame': {'step_seconds': 1}}, run_id=CID)
        service.events.publish('state', {**service.load('user', 'during-' + CID, CID), 'status': 'completed'}, run_id=CID)
        return service.events.after(0, run_id=CID)
    handler.wfile = io.BytesIO()
    with patch.object(service.events, 'wait_after', side_effect=updates):
        handler._stream_class2('user', 'during-' + CID, CID)
    assert b'"type": "frame"' in handler.wfile.getvalue()
    assert not gateway.commands


def test_stop_does_not_run_the_remaining_queue(setup):
    service, gateway, _, body, _ = setup
    gateway.on_step = lambda _: service.control('user', 'during-' + CID, CID, 'stop', {'executionVersion': 1})
    start(service, body); service.execute(CID)
    result = service.load('user', 'during-' + CID, CID)
    assert result['status'] == 'stopped'
    assert all(r['status'] == 'cancelled' for r in result['runs'])
    assert len(gateway.preparations) == 1


def test_a_foreign_engine_or_lease_is_never_closed_or_replaced(setup):
    service, gateway, repo, body, _ = setup
    gateway.state, gateway.run_id = 'PAUSED', 'foreign-run'
    start(service, body); service.execute(CID)
    assert service.load('user', 'during-' + CID, CID)['status'] == 'failed'
    assert [c['action'] for c in gateway.commands] == ['get_status']
    gateway.commands.clear()
    repo.acquire_gateway_lease('another-owner', ttl_seconds=180)
    service.control('user', 'during-' + CID, CID, 'retry', {'executionVersion': 1}); service.execute(CID)
    assert not gateway.commands
    assert repo._memory['leases'][0]['owner_id'] == 'another-owner'


def test_first_class_worker_cannot_disconnect_the_comparison_lease(setup):
    from crowdsim.experiments.orchestrator import ExperimentOrchestrator
    _, gateway, repo, _, _ = setup
    repo.acquire_gateway_lease('class2-comparison-test', ttl_seconds=180)
    group = repo.create_experiment('user', 'during-other', {'scenario': {}, 'duringObservation': {'conversationId': 'other', 'snapshotId': 's'}})
    run = repo.create_run('user', 'during-other', group['experiment_id'], 'C0', 1, {})
    orchestrator = ExperimentOrchestrator(repo, SimpleNamespace(add_listener=lambda _: None, **gateway.__dict__), SimpleNamespace())
    # Use the actual fake transport methods, without opening a connection.
    orchestrator.gateway = gateway
    orchestrator._queue.put({'user_id': 'user', 'workspace_id': 'during-other', 'experiment': group, 'run': run})
    orchestrator._queue.put(None)
    orchestrator._worker_loop()
    assert gateway.closed == 0 and not gateway.commands
    assert repo._memory['leases'][0]['owner_id'] == 'class2-comparison-test'


def test_http_handlers_route_only_during_comparisons_without_listener(setup):
    from crowdsim.experiments.serve import ControlRequestHandler
    service, _, _, body, _ = setup
    h = ControlRequestHandler.__new__(ControlRequestHandler)
    h.server = SimpleNamespace(bundle=SimpleNamespace(class2_comparisons=service))
    h.path = '/crowdSim/control/during-class2/conversations/' + CID + '/comparisons'
    h.headers = {'X-CrowdSim-User': 'user', 'X-CrowdSim-Workspace': 'during-' + CID}
    h._json_body = lambda: body
    responses = []
    h._send_json = lambda status, data, **_: responses.append((status, data))
    h.do_POST(); assert responses[-1][0] == 202
    assert responses[-1][1]['status'] == 'ready' and service._active is None
    h.do_GET(); assert responses[-1][1]['conversationId'] == CID
    h.path += '/start'; h._json_body = lambda: {'executionVersion': 1}
    h.do_POST(); assert responses[-1][1]['status'] == 'queued'
    h.path = h.path.removesuffix('/start') + '/runtime'
    h.do_GET(); assert responses[-1][1]['record']['conversationId'] == CID


def test_normal_configuration_builds_a_new_runtime_for_a_changed_class2_binding():
    from crowdsim.infrastructure.websocket_server import OverlayServer
    from crowdsim.core.simulation_runtime import RuntimeState
    from tests.test_retained_simulation_session import Runtime, Client
    old = Runtime(); old.state = RuntimeState.CLOSED; old.random_seed = 1
    record = old.record('req-seed-2')
    record['requirement']['class2_demo'] = {'seed': 2}
    created = []
    def factory(receipt):
        runtime = Runtime(); runtime.state = RuntimeState.CREATED
        runtime.random_seed = receipt['requirement']['class2_demo']['seed']
        runtime.demand_mode = 'fixed'
        runtime.configure_demand = lambda _: None
        runtime.initialize = lambda: runtime.reset(2)
        created.append(runtime)
        return runtime
    server = OverlayServer(old, 'unused', 0, requirement_repository=SimpleNamespace(load=lambda _: record), runtime_factory=factory)
    server.client = Client()
    asyncio.run(server._handle_message(json.dumps({'action': 'configure', 'requirement_id': 'req-seed-2', 'count': 2, 'request_id': 'new-seed'})))
    assert len(created) == 1 and server.simulator is created[0]
    assert created[0].random_seed == 2 and old.closed == 1
    assert server.client.messages[-1]['type'] == 'init'
    # Normal configure does not replace an active run with another requirement.
    server.client.messages.clear()
    asyncio.run(server._handle_message(json.dumps({'action': 'configure', 'requirement_id': 'req-seed-3', 'count': 2, 'request_id': 'active'})))
    assert server.client.messages[-1]['type'] == 'error'
    assert len(created) == 1 and created[0].closed == 0
