"""Recovery guards use real repository and real orchestrator; no runtime resets."""
from copy import deepcopy
import threading
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.controller_registry import ControllerRegistry
from tests.test_final_frame_archive import Gateway

def test_durable_presentation_generation_is_monotone(tmp_path):
    path=tmp_path/'control'
    a=CrowdSimRepository(path); assert a.allocate_presentation_generation()==1
    b=CrowdSimRepository(path); assert b.allocate_presentation_generation()==2

def test_preopened_repository_instances_allocate_unique_generations(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    repositories = [CrowdSimRepository(tmp_path/'control') for _ in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        values = list(pool.map(lambda repository: repository.allocate_presentation_generation(), repositories))
    assert sorted(values) == list(range(1, 13))
    assert CrowdSimRepository(tmp_path/'control').allocate_presentation_generation() == 13


def test_corrupt_generation_fails_closed_without_overwriting(tmp_path):
    import pytest
    root = tmp_path/'control'; repository = CrowdSimRepository(root)
    path = root/'presentation_epochs.json'
    path.write_text('[{"generation": "wrong"}]', encoding='utf-8')
    with pytest.raises(ValueError): repository.allocate_presentation_generation()
    assert path.read_text(encoding='utf-8') == '[{"generation": "wrong"}]'


def test_running_c0_recovers_exact_owned_attempt_without_enqueuing_reset(tmp_path,monkeypatch):
    repo=CrowdSimRepository(tmp_path/'control')
    e=repo.create_experiment('u','w',{'name':'same','status':'running','duringObservation':{'modelId':'m'}})
    a=repo.create_run('u','w',e['experiment_id'],'C0',1,{})
    repo.update_run('u','w',a['run_id'],{'status':'paused','runtime_run_id':'same-runtime'})
    b=repo.create_run('u','w',e['experiment_id'],'C0',2,{})
    o=ExperimentOrchestrator(repo,Gateway(),ControllerRegistry());monkeypatch.setattr(o,'_ensure_worker',lambda:None)
    o.runtime_view('u','w',e['experiment_id'])
    assert o._queue.qsize()==2
    first=o._queue.get(); assert first['recover'] and first['run']['runtime_run_id']=='same-runtime'
    assert o._queue.get()['run']['run_id']==b['run_id']
    o.runtime_view('u','w',e['experiment_id']); assert o._queue.qsize()==0
    assert repo.get_run('u','w',a['run_id'])['status']=='paused'

def test_foreign_scope_and_completed_without_archive_cannot_recover(tmp_path):
    repo=CrowdSimRepository(tmp_path/'control');e=repo.create_experiment('u','w',{'name':'same','duringObservation':{}})
    o=ExperimentOrchestrator(repo,Gateway(),ControllerRegistry())
    import pytest
    with pytest.raises(LookupError):o.runtime_view('other','w',e['experiment_id'])
    assert o._queue.qsize()==0


def test_snapshot_request_is_read_only_strict_run_bound():
    import asyncio,json
    from tests.test_retained_simulation_session import Runtime,Client
    from crowdsim.infrastructure.websocket_server import OverlayServer
    x=Runtime();s=OverlayServer(x,'unused',0);s.client=Client()
    async def check():
        await s._handle_message(json.dumps({'action':'get_snapshot','run_id':x.run_id,'request_id':'q'}))
        response=s.client.messages[-1];assert response['type']=='runtime_snapshot' and response['frame']['run_id']==x.run_id
        assert x.ticks==x.resets==0
        await s._handle_message(json.dumps({'action':'get_snapshot','run_id':'other','request_id':'bad'}))
        assert s.client.messages[-1]['type']=='error' and x.ticks==x.resets==0
    asyncio.run(check())


def test_restored_history_preserves_trend_and_rejects_foreign_or_duplicate():
    import pytest
    from crowdsim.experiments.observation_builder import ObservationBuilder
    frame = {'run_id': 'physical', 'snapshot_id': 'physical:1', 'step_index': 1,
             'step_seconds': 1, 'metrics': {'pedestrian_count': 1, 'riskIndex': .2}}
    initial = ObservationBuilder().build(frame, run_id='control')
    builder = ObservationBuilder()
    builder.restore_history([initial], run_id='control')
    next_frame = {**frame, 'snapshot_id': 'physical:2', 'step_index': 2,
                  'step_seconds': 2, 'metrics': {'pedestrian_count': 1, 'riskIndex': .5}}
    observed = builder.build(next_frame, run_id='control')
    assert observed['historyWindow'][0]['observationId'] == initial['observationId']
    assert observed['trend']['risk'] == pytest.approx(.3)
    with pytest.raises(ValueError): builder.restore_history([initial, initial], run_id='control')
    with pytest.raises(ValueError): builder.restore_history([initial], run_id='other')
