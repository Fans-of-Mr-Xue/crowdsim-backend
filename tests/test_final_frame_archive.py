"""Final frames: strict binding, restart isolation, persistence, and no gateway writes."""
from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace
import pytest
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.final_frame_archive import FinalFrameArchive
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.controller_registry import ControllerRegistry
from crowdsim.experiments.serve import ControlRequestHandler, CONTROL_PREFIX

class Gateway:
    def add_listener(self, callback): self.callback = callback
    def submit(self, payload): raise AssertionError('read must not command SUMO')

@pytest.fixture
def sample(tmp_path):
    repo = CrowdSimRepository(tmp_path / 'control')
    binding = dict(conversationId='check', snapshotId='s', requirementId='req', requirementFingerprint='sha256:req', modelId='model', modelFingerprint='sha256:model')
    e = repo.create_experiment('u','w',dict(name='final', duringObservation=binding))
    r = repo.create_run('u','w',e['experiment_id'],'C0',1,{},formal=True)
    r = repo.update_run('u','w',r['run_id'],dict(status='completed',runtime_run_id='runtime'))
    f = dict(type='update',run_id='runtime',runtime_state='FINISHED',snapshot_id='runtime:24',step_seconds=12,vehicles=[],pedestrians=[dict(id='p',state=dict(stress=.1,agent_runtime=dict(run_id='runtime',snapshot_id='runtime:24',time_seconds=12,person_id='p',model_id='model',model_fingerprint='sha256:model')))])
    v = dict(runId=r['run_id'],init=dict(run_id='runtime',requirement=dict(requirement_id='req',fingerprint='sha256:req'),artificial_society=dict(model_id='model',fingerprint='sha256:model')),frame=f)
    digest=repo.save_final_frame(e,r,v)
    r=repo.update_run('u','w',r['run_id'],dict(finalFrameSha256=digest))
    return repo,e,r,v

def test_read_is_exact_detached_persistent_and_does_not_connect(sample):
    repo,e,r,v=sample
    o=ExperimentOrchestrator(repo,Gateway(),ControllerRegistry())
    x=o.final_frame_view('u','w',e['experiment_id'],r['run_id']);assert x['frame']==v['frame'] and x['frameAvailable']
    x['frame']['pedestrians'][0]['state']['stress']=99
    assert o.final_frame_view('u','w',e['experiment_id'],r['run_id'])['frame']==v['frame']
    reloaded=CrowdSimRepository(repo._root)
    assert ExperimentOrchestrator(reloaded,Gateway(),ControllerRegistry()).final_frame_view('u','w',e['experiment_id'],r['run_id'])['frame']==v['frame']

@pytest.mark.parametrize('wrong',['user','workspace','experiment','run'])
def test_foreign_scope_never_reads(sample,wrong):
    repo,e,r,v=sample;o=ExperimentOrchestrator(repo,Gateway(),ControllerRegistry())
    args=['u','w',e['experiment_id'],r['run_id']];args[['user','workspace','experiment','run'].index(wrong)]='foreign'
    with pytest.raises(LookupError):o.final_frame_view(*args)

@pytest.mark.parametrize('patch',[dict(status='running'),dict(status='failed'),dict(restartPending=True)])
def test_incomplete_or_restart_not_displayed(sample,patch):
    repo,e,r,v=sample;repo.update_run('u','w',r['run_id'],patch)
    with pytest.raises(ValueError):ExperimentOrchestrator(repo,Gateway(),ControllerRegistry()).final_frame_view('u','w',e['experiment_id'],r['run_id'])

def test_no_archive_returns_empty_not_latest_and_restart_never_inherits(sample):
    repo,e,r,v=sample
    newer=repo.update_run('u','w',r['run_id'],dict(runtime_run_id='new-runtime',restartCount=1,finalFrameSha256=None))
    o=ExperimentOrchestrator(repo,Gateway(),ControllerRegistry());o._runtime_view=v
    x=o.final_frame_view('u','w',e['experiment_id'],r['run_id'])
    assert x['frameAvailable'] is False and 'frame' not in x and 'init' not in x

@pytest.mark.parametrize('kind',['run','snapshot','model','time','duplicate','synthetic','agent'])
def test_bad_frame_rejected(sample,kind):
    repo,e,r,v=sample;v=deepcopy(v)
    if kind=='run':v['frame']['run_id']='other'
    elif kind=='snapshot':v['frame']['snapshot_id']='other:24'
    elif kind=='model':v['init']['artificial_society']['fingerprint']='other'
    elif kind=='time':v['frame']['step_seconds']=float('nan')
    elif kind=='duplicate':v['frame']['pedestrians']*=2
    elif kind=='synthetic':v['frame']['pedestrians'][0]['synthetic']=True
    elif kind=='agent':v['frame']['pedestrians'][0]['state']['agent_runtime']['model_id']='other'
    with pytest.raises(ValueError):repo.save_final_frame(e,r,v)

def test_conflicting_same_identity_cannot_overwrite(sample):
    repo,e,r,v=sample;v=deepcopy(v);v['frame']['pedestrians'][0]['state']['stress']=.8
    with pytest.raises(ValueError,match='CONFLICT'):repo.save_final_frame(e,r,v)

def test_tampered_archive_and_row_hash_fail_closed(sample):
    repo,e,r,v=sample;p=next((repo._root/'final_frames').glob('*.json'));record=json.loads(p.read_text());record['view']['frame']['pedestrians'][0]['state']['stress']=4;p.write_text(json.dumps(record))
    with pytest.raises(ValueError,match='ARCHIVE_INVALID'):repo.read_final_frame(e,r)

def test_write_failure_leaves_no_record_or_success_pointer(sample,monkeypatch):
    repo,e,r,v=sample;r={**r,'runtime_run_id':'another'};v=deepcopy(v);v['init']['run_id']='another';v['frame']['run_id']='another';v['frame']['snapshot_id']='another:24';v['frame']['pedestrians']=[]
    import crowdsim.experiments.final_frame_archive as m
    monkeypatch.setattr(m.os,'fsync',lambda _:(_ for _ in ()).throw(OSError('write failed')))
    with pytest.raises(OSError):repo.save_final_frame(e,r,v)
    assert len(list((repo._root/'final_frames').glob('*.json')))==1 and not list((repo._root/'final_frames').glob('*.tmp'))

def test_http_endpoint_returns_scoped_read(sample):
    repo,e,r,v=sample;o=ExperimentOrchestrator(repo,Gateway(),ControllerRegistry());h=object.__new__(ControlRequestHandler)
    h.server=SimpleNamespace(bundle=SimpleNamespace(orchestrator=o));h.path=f"{CONTROL_PREFIX}/experiments/{e['experiment_id']}/runs/{r['run_id']}/final-frame";h.headers={'X-CrowdSim-User':'u','X-CrowdSim-Workspace':'w'}
    out=[];h._send_json=lambda *a,**k:out.append(a);h.do_GET();assert out[0][0]==200 and out[0][1]['frame']==v['frame']
