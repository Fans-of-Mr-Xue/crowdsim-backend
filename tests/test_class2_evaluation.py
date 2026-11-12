"""Archived evidence and fake-engine checks only. No server, model or SUMO is started."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from crowdsim.experiments.class2_comparison import Class2ComparisonService, ComparisonConflict
from crowdsim.experiments.class2_evaluation import topsis, _measure
from crowdsim.scenarios.class2_flow_runtime import capture_flow_state
from tests.test_class2_comparison import setup, start, CID  # noqa: F401


def measured(setup):
    service, gateway, _, body, _ = setup
    # One actual intervention and one observe-only candidate: no fabricated comparison profiles.
    setup[4]['artifacts']['generation']['payload']['options'][0]['actions'][0]['actionType'] = 'publish_guidance'
    setup[4]['artifacts']['generation']['payload']['options'][0]['actions'][0]['parameters'] = {'message': '测试消息'}
    original = gateway.frame
    def frame():
        value = original()
        arrived = 70 if (gateway.plan or {}).get('actions', [{}])[0].get('actionId') == 'one' else 90
        value['metrics']['class2_flows'] = {'evaluation': {
            'total': 100, 'arrived': arrived, 'plannedWaiting': 5, 'slowWaiting': 3,
            'meanTravelSpeedMps': 1.2, 'flows': [{'id': 'user-flow', 'name': '用户人流', 'total': 100,
                'arrived': arrived, 'plannedWaiting': 5, 'slowWaiting': 3}]}}
        return value
    gateway.frame = frame
    start(service, body); service.execute(CID)
    return service, gateway


def test_real_archives_produce_paired_statistics_and_topsis_without_engine_access(setup):
    service, gateway = measured(setup)
    before = deepcopy(gateway.commands)
    result = service.evaluation('user', 'during-' + CID, CID)
    assert gateway.commands == before
    assert len(result['commonSeeds']) == 2
    assert all(r['valid'] and r['sampleCount'] == 15 for r in result['runs'])
    a, b = result['candidates']
    assert a['metrics']['arrival-ratio'] == {'n': 2, 'mean': 70, 'std': 0, 'min': 70, 'max': 70}
    assert result['pairwise'][0]['metrics']['arrival-ratio']['mean'] == -20
    assert b['score'] > a['score'] and b['rank'] == 1
    assert result['baseline']['candidateId'] == 'b'
    assert not any(d['key'] in {'global-speed', 'local-speed'} for d in result['ranking']['criteria'])
    assert 'publish_guidance' in result['toolEffects']


def test_integrates_native_intervals_and_does_not_invent_missing_data():
    definitions = []
    samples = [{'timeSeconds': .5, 'flowState': {'total': 10, 'arrived': 2, 'plannedWaiting': 4, 'slowWaiting': 2}},
               {'timeSeconds': 1, 'flowState': {'total': 10, 'arrived': 5, 'plannedWaiting': 2, 'slowWaiting': 0}}]
    assert _measure(samples, definitions, 0) == {'arrival-ratio': 50, 'planned-wait': 3, 'slow-wait': 1}
    assert _measure([{'timeSeconds': .5}], definitions, 0) == {}


def test_topsis_has_real_benefit_cost_directions_ties_and_single_decision():
    definitions = [{'key': 'speed', 'higherIsBetter': True}, {'key': 'density', 'higherIsBetter': False}]
    result = topsis([{'speed': 2, 'density': 1}, {'speed': 1, 'density': 3}], definitions)
    assert result['scores'] == pytest.approx([100, 0])
    assert sum(d['weight'] for d in result['criteria']) == 1
    assert topsis([{'speed': 1}, {'speed': 1}], definitions)['scores'] == [50, 50]
    assert topsis([{'speed': 1}], definitions)['scores'] == [None]
    assert topsis([{}, {}], definitions)['scores'] == [None, None]


def test_no_uncontrolled_candidate_means_no_invented_baseline_and_zero_denominators_are_missing(setup):
    service, _ = measured(setup)
    record = service._read(CID)
    for candidate in record['candidates']:
        candidate['actions'][0]['actionType'] = 'publish_guidance'
    service._write(record)
    assert not service.evaluation('user', 'during-' + CID, CID)['baseline']['available']
    record['candidates'][1]['actions'][0]['actionType'] = 'observe_only'
    for row in record['runs']:
        if row['candidateId'] != 'b':
            continue
        path = service.root / row['sampleArchive']
        samples = [json.loads(line) for line in path.read_text().splitlines()]
        for sample in samples:
            sample['flowState']['plannedWaiting'] = 0
        path.write_text(''.join(json.dumps(s)+'\n' for s in samples))
    service._write(record)
    baseline = service.evaluation('user', 'during-' + CID, CID)['baseline']
    assert baseline['available']
    assert baseline['comparisons'][0]['improvementPercent']['planned-wait']['n'] == 0


def test_state_measurement_separates_destination_holds_planned_waits_and_slow_travel():
    people = {p: {'flow_id': 'f'} for p in ['arrived', 'held', 'slow', 'walking']}
    flows = SimpleNamespace(dwelling={'arrived'}, retention_pending={}, people=people,
                            layout={'flow_design': {'flows': [{'id': 'f', 'name': '人流', 'count': 4}]}})
    motions = {p: SimpleNamespace(person_id=p, speed=1 if p == 'walking' else 0) for p in people}
    runtime = SimpleNamespace(plan_executor=SimpleNamespace(flow_hold_reasons={'held': {'release'}}, activity_hold_until={'arrived': None}),
        visual_policy=SimpleNamespace(low_speed_threshold_mps=.2, confirmation_duration_seconds=3),
        current=SimpleNamespace(persons=motions), population=SimpleNamespace(states={p: SimpleNamespace(low_speed_duration=10) for p in people}))
    value = capture_flow_state(runtime, flows)
    assert (value['arrived'], value['plannedWaiting'], value['slowWaiting'], value['meanTravelSpeedMps']) == (1, 1, 1, .5)
    assert value['flows'][0]['total'] == 4
    assert runtime.plan_executor.flow_hold_reasons == {'held': {'release'}}


def test_control_receipts_record_status_times_without_changing_execution():
    from crowdsim.scenarios.class2_control import Class2Control
    control = Class2Control.__new__(Class2Control)
    runtime = SimpleNamespace(time_seconds=3, recorder=None)
    row = {'action': {'actionId': 'a', 'actionType': 'observe_only'}, 'status': 'scheduled',
           'seen': set(), 'held': set(), 'detail': {}, 'speedErrors': set()}
    control._record(runtime, row, 'active')
    runtime.time_seconds = 3.5
    control._record(runtime, row, 'active')
    assert control._receipt(row)['statusChangedAtSeconds'] == 3
    runtime.time_seconds = 4
    control._record(runtime, row, 'completed')
    assert control._receipt(row)['statusChangedAtSeconds'] == 4


def test_timeline_prefers_actual_receipt_times_and_leaves_missing_before_window_empty(setup):
    service, _ = measured(setup)
    row = service._read(CID)['runs'][0]
    path = service.root / row['sampleArchive']
    samples = [json.loads(line) for line in path.read_text().splitlines()]
    samples[0]['actionUpdates'] = []
    samples[1]['actionUpdates'] = [{'actionId': 'one', 'status': 'completed', 'statusChangedAtSeconds': 3}]
    path.write_text(''.join(json.dumps(s)+'\n' for s in samples))
    result = service.evaluation('user', 'during-' + CID, CID)['runs'][0]
    event = next(e for e in result['actionEvents'] if e['actionId'] == 'one')
    assert (event['timeSeconds'], event['observedAtSeconds'], event['timeSource']) == (3, 3.5, 'receipt')
    window = result['actionWindows'][0]
    assert window['metrics']['global-density']['before']['n'] == 0
    assert window['metrics']['global-density']['before']['mean'] is None


@pytest.mark.parametrize('problem', ['failed', 'missing', 'truncated', 'unaligned'])
def test_incomplete_or_unaligned_rounds_never_become_zero_samples(setup, problem):
    service, _ = measured(setup)
    record = service._read(CID)
    row = record['runs'][1]
    path = service.root / row['sampleArchive']
    if problem == 'failed':
        row['status'] = 'failed'; service._write(record)
    elif problem == 'missing':
        path.unlink()
    else:
        samples = [json.loads(line) for line in path.read_text().splitlines()]
        if problem == 'truncated':
            samples.pop()
        else:
            samples[0]['timeSeconds'] += .1
        path.write_text(''.join(json.dumps(s)+'\n' for s in samples))
    result = service.evaluation('user', 'during-' + CID, CID)
    assert result['commonSeeds'] == [20260908]
    assert result['pairwise'][0]['metrics']['arrival-ratio']['n'] == 1
    assert result['pairwise'][0]['metrics']['arrival-ratio']['std'] is None
    if problem != 'unaligned':
        assert result['candidates'][0]['completedRuns'] == 1


def test_save_is_durable_scoped_and_rejects_stale_or_fallback_reviews(setup):
    service, _ = measured(setup)
    result = service.evaluation('user', 'during-' + CID, CID)
    review = {'conversationId': CID, 'snapshotId': result['snapshotId'], 'candidateId': 'a', 'skillId': 'strategy-alignment', 'fingerprint': result['fingerprint'], 'total': 80, 'online': True}
    body = {'conversationId': CID, 'snapshotId': result['snapshotId'], 'fingerprint': result['fingerprint'], 'selectedCandidateId': 'a', 'reviews': {'a/strategy-alignment': review}}
    saved = service.evaluation('user', 'during-' + CID, CID, body)
    assert saved['saved']['reviews'] == body['reviews']
    assert service.evaluation('user', 'during-' + CID, CID)['saved']['fingerprint'] == result['fingerprint']
    with pytest.raises(ValueError):
        service.evaluation('other-user', 'during-' + CID, CID)
    with pytest.raises(ValueError):
        service.evaluation('user', 'during-other', CID)
    with pytest.raises(ValueError):
        service.evaluation('user', 'during-' + CID, CID, {**body, 'reviews': {'a/strategy-alignment': {**review, 'online': False}}})
    for field, value in [('conversationId', 'another-conversation'), ('snapshotId', 'another-snapshot')]:
        with pytest.raises(ValueError, match='会话或确认快照不一致'):
            service.evaluation('user', 'during-' + CID, CID, {**body, field: value})
        with pytest.raises(ValueError, match='来源或格式不一致'):
            service.evaluation('user', 'during-' + CID, CID, {**body, 'reviews': {'a/strategy-alignment': {**review, field: value}}})
    assert service.evaluation('user', 'during-' + CID, CID)['saved']['reviews'] == body['reviews']
    record = service._read(CID); record['runs'][0].update(attempt=2, status='running'); service._write(record)
    assert service.evaluation('user', 'during-' + CID, CID)['savedOutdated']
    with pytest.raises(ComparisonConflict):
        service.evaluation('user', 'during-' + CID, CID, body)


def test_evaluation_http_routes_are_read_only_and_do_not_use_websocket(setup):
    from crowdsim.experiments.serve import ControlRequestHandler
    service, gateway = measured(setup)
    calls = len(gateway.commands)
    handler = ControlRequestHandler.__new__(ControlRequestHandler)
    handler.server = SimpleNamespace(bundle=SimpleNamespace(class2_comparisons=service))
    handler._path = lambda: ('/crowdSim/control/during-class2/conversations/' + CID + '/comparisons/evaluation', {})
    handler._context = lambda: ('user', 'during-' + CID)
    responses = []
    handler._send_json = lambda status, data, **_: responses.append((status, data))
    handler.do_GET()
    assert responses[-1][0] == 200 and responses[-1][1]['snapshotId'] == 'snapshot-test'
    handler._json_body = lambda: {'conversationId': CID, 'snapshotId': 'snapshot-test', 'fingerprint': responses[0][1]['fingerprint'], 'selectedCandidateId': 'a'}
    handler.do_POST()
    assert responses[-1][0] == 200 and responses[-1][1]['saved']['selectedCandidateId'] == 'a'
    assert len(gateway.commands) == calls


def test_incremental_review_saves_keep_all_decisions_and_survive_service_recreation(setup):
    service, gateway = measured(setup)
    result = service.evaluation('user', 'during-' + CID, CID)
    commands = deepcopy(gateway.commands)
    reviews = {}
    for candidate in ['a', 'b']:
        for skill in ['strategy-alignment', 'risk-red-team', 'execution-readiness', 'multi-agent-review']:
            key = candidate + '/' + skill
            reviews[key] = {'candidateId': candidate, 'skillId': skill, 'fingerprint': result['fingerprint'], 'total': 80, 'online': True}
            body = {'conversationId': CID, 'snapshotId': result['snapshotId'], 'fingerprint': result['fingerprint'],
                    'selectedCandidateId': candidate, 'reviews': deepcopy(reviews)}
            saved = service.evaluation('user', 'during-' + CID, CID, body)['saved']
            assert set(saved['reviews']) == set(reviews)
            assert all(r['conversationId'] == CID and r['snapshotId'] == result['snapshotId'] for r in saved['reviews'].values())
            reviews = saved['reviews']
    reopened = Class2ComparisonService(service.root, service.repository, gateway, service.requirements, service.artifacts, gateway.prepare, auto_start=False)
    try:
        assert reopened.evaluation('user', 'during-' + CID, CID)['saved']['reviews'] == reviews
        assert gateway.commands == commands
    finally:
        reopened.close()
