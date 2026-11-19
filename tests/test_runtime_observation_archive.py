"""Source observation archive independent of optional psychology metrics."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json,tempfile,unittest
from unittest.mock import patch
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
from tests.test_observed_feedback_stage_five import fixtures

class RuntimeObservationArchiveTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  root=Path(self.temp.name);cfg=root/'test.sumocfg';cfg.write_text('<configuration/>')
  with patch('crowdsim.infrastructure.experiment_recorder.subprocess.run',side_effect=OSError):
   self.recorder=ExperimentRecorder('r',cfg,{},root=root)
  self.addCleanup(lambda:self.recorder.abort('fixture closed'))
  p,s,o=fixtures()
  self.r=SimpleNamespace(run_id='r',snapshot_id=o.snapshot_id,time_seconds=o.time_seconds,
   current=SimpleNamespace(time_seconds=o.time_seconds,persons={'p':o.own_motion}),observations={'p':o},
   population=SimpleNamespace(states={'p':s},profile_for=lambda pid:p))
 def rows(self):return list(map(json.loads,(self.recorder.directory/'runtime_observations.jsonl').read_text().splitlines()))
 def test_current_observation_not_state_or_metric_risk_is_archived_unrounded(self):
  self.r.population.states['p'].perceived_risk=.9
  self.r.observations['p']=replace(self.r.observations['p'],perceived_risk=.123456789012345)
  self.recorder.record_runtime_observations(self.r)
  x=self.rows()[0];self.assertEqual(.123456789012345,x['perceived_risk']);self.assertEqual(.9,x['state']['perceived_risk'])
  self.assertEqual('observed',x['status']);self.assertEqual('runtime.observations',x['source'])
  self.assertEqual('runtime-observation-source-v1',json.loads((self.recorder.directory/'manifest.json').read_text())['runtime_observations']['version'])
 def test_missing_observation_is_explicit_null_not_default_zero(self):
  self.r.observations={};self.recorder.record_runtime_observations(self.r)
  x=self.rows()[0];self.assertEqual('missing_observation',x['status']);self.assertIsNone(x['perceived_risk'])
 def test_stale_run_person_time_snapshot_or_motion_is_not_used(self):
  for key,change in [('person_id','other'),('snapshot_id','old:1'),('time_seconds',0),('own_motion',replace(self.r.current.persons['p'],x=2))]:
   with self.subTest(key=key),tempfile.TemporaryDirectory() as d:
    self.recorder.directory=Path(d);(Path(d)/'manifest.json').write_text('{}');self.recorder._runtime_observation_registered=False;self.recorder._runtime_observation_last=None
    self.r.observations['p']=replace(fixtures()[2],**{key:change});self.recorder.record_runtime_observations(self.r)
    self.assertEqual('observation_binding_mismatch',self.rows()[0]['status']);self.assertIsNone(self.rows()[0]['perceived_risk'])
 def test_nonfinite_or_out_of_range_observation_is_explicit_unavailable(self):
  for value in [float('nan'),float('inf'),-1,1.1,True]:
   self.r.observations['p']=replace(fixtures()[2],perceived_risk=value);self.recorder._runtime_observation_last=None
   self.recorder.record_runtime_observations(self.r);self.assertEqual('invalid_observation_values',self.rows()[-1]['status']);self.assertIsNone(self.rows()[-1]['perceived_risk'])
 def test_missing_density_stays_null_without_losing_measured_zero_risk(self):
  self.r.observations['p']=replace(fixtures()[2],objective_density_per_m2=None,perceived_risk=0)
  self.recorder.record_runtime_observations(self.r);x=self.rows()[0]
  self.assertEqual('observed',x['status']);self.assertEqual(0,x['perceived_risk']);self.assertIsNone(x['objective_density'])
 def test_duplicate_boundary_is_once_and_conflicting_same_time_is_rejected(self):
  self.recorder.record_runtime_observations(self.r);self.recorder.record_runtime_observations(self.r);self.assertEqual(1,len(self.rows()))
  self.r.observations['p']=replace(fixtures()[2],perceived_risk=.4)
  with self.assertRaisesRegex(ValueError,'conflicting'):self.recorder.record_runtime_observations(self.r)
 def test_old_physical_time_or_run_boundary_rejected(self):
  self.recorder.record_runtime_observations(self.r)
  self.r.snapshot_id='r:0';self.r.time_seconds=0
  p,s,o=fixtures(0);self.r.current=SimpleNamespace(time_seconds=0,persons={'p':o.own_motion});self.r.observations={'p':o}
  with self.assertRaisesRegex(ValueError,'stale'):self.recorder.record_runtime_observations(self.r)
  self.r.snapshot_id='other:0'
  with self.assertRaisesRegex(ValueError,'boundary'):self.recorder.record_runtime_observations(self.r)
 def test_actual_motion_identity_or_nonfinite_refuses_before_append(self):
  for motion in [replace(fixtures()[2].own_motion,person_id='wrong'),replace(fixtures()[2].own_motion,x=float('nan'))]:
   self.r.current.persons={'p':motion}
   with self.assertRaises(ValueError):self.recorder.record_runtime_observations(self.r)
  self.assertFalse((self.recorder.directory/'runtime_observations.jsonl').exists())
 def test_failed_partial_append_rolls_back_and_retry_records_once(self):
  original=Path.open;dest=self.recorder.directory/'runtime_observations.jsonl'
  class FailWrite:
   def __init__(self,h):self.h=h
   def __enter__(self):return self
   def __exit__(self,*a):return self.h.__exit__(*a)
   def __getattr__(self,k):return getattr(self.h,k)
   def write(self,s):self.h.write(s[:30]);raise OSError('partial write')
  def broken(p,*a,**kw):
   h=original(p,*a,**kw);return FailWrite(h) if p==dest and a and a[0]=='a+' else h
  with patch.object(Path,'open',broken),self.assertRaises(OSError):self.recorder.record_runtime_observations(self.r)
  self.assertIsNone(self.recorder._runtime_observation_last);self.assertEqual('',dest.read_text())
  self.recorder.record_runtime_observations(self.r);self.assertEqual(1,len(self.rows()))
 def test_other_run_never_writes_into_previous_recorder(self):
  self.r.run_id='new';self.r.snapshot_id='new:1'
  self.r.observations['p']=replace(fixtures()[2],snapshot_id='new:1')
  with self.assertRaisesRegex(ValueError,'boundary'):self.recorder.record_runtime_observations(self.r)
  self.assertFalse((self.recorder.directory/'runtime_observations.jsonl').exists())
 def test_profile_from_another_person_refuses_archive(self):
  self.r.population.profile_for=lambda pid:replace(fixtures()[0],person_id='other')
  with self.assertRaisesRegex(ValueError,'profile binding'):self.recorder.record_runtime_observations(self.r)
  self.assertFalse((self.recorder.directory/'runtime_observations.jsonl').exists())
 def test_no_learning_record_step_does_not_enable_source_archive(self):
  with patch.object(self.recorder,'record_runtime_observations') as archive:
   with self.assertRaises(AttributeError):self.recorder.record_step(self.r,self.r.current,{})
   archive.assert_not_called()
 def test_record_step_calls_source_archive_for_attached_learning(self):
  self.r.learning_controller=object()
  with patch.object(self.recorder,'record_runtime_observations') as archive:
   with self.assertRaises(AttributeError):self.recorder.record_step(self.r,self.r.current,{})
   archive.assert_called_once_with(self.r,self.r.current)
 def test_snapshot_empty_persons_commits_a_bounded_guard(self):
  self.r.current.persons={};self.recorder.record_runtime_observations(self.r)
  self.recorder.record_runtime_observations(self.r);self.assertEqual([],self.rows())

if __name__=='__main__':unittest.main()
