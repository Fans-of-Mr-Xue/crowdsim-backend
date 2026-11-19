"""Read-only frame state does not evaluate affect, allocate learners or act."""
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from crowdsim.infrastructure.agent_runtime_view import build_agent_runtime_view
from crowdsim.decision.plan_executor import PlanExecutor
from crowdsim.modeling_algorithm.contracts import PADState,DesireState
from crowdsim.domain.crowdsim_models import BehaviorPlan,PlanExecutionResult
from tests.test_runtime_learning_stage_five import runtime_stub,begin,next_step,local

class AgentRuntimeViewTests(unittest.TestCase):
 def setUp(self):
  self.r,self.c=runtime_stub('tabular_q_v1');self.r.learning_controller=self.c
  self.p=self.r.population.profile_for('p');self.s=self.r.population.states['p']
 def view(self):return build_agent_runtime_view(self.r,self.p,self.s)
 def test_no_reads_allocate_learner_or_update_affect_or_pipeline(self):
  engine=self.r.decision_engine
  before=(deepcopy(engine.emotional_states),deepcopy(engine._boundary_contexts),self.c.diagnostics())
  v=self.view();self.assertEqual('not_instantiated',v['learning']['status']);self.assertEqual(0,v['learning']['update_version'])
  self.assertEqual('disabled',v['affect']['status']);self.assertIsNone(v['last_execution'])
  for _ in range(3):self.assertEqual(v,self.view())
  self.assertEqual(before,(engine.emotional_states,engine._boundary_contexts,self.c.diagnostics()))
 def test_explicit_role_configuration_is_detached_not_global_implementation_labels(self):
  v=self.view();self.assertEqual('tabular_q_v1',v['learning']['method_id'])
  self.assertEqual(self.r.decision_engine.assemblies[self.p.crowd_role].order,tuple(x['id'] for x in v['configuration']['architecture']))
  v['configuration']['capabilities'].clear();self.assertTrue(self.view()['configuration']['capabilities']['learning'])
 def test_real_learner_update_is_own_person_not_run_total(self):
  begin(self.r,self.c);next_step(self.r,2);self.s=self.r.population.states['p'];self.c.advance(self.r,timeline_end=False);self.c.refine(self.r,local(self.r))
  v=self.view();self.assertEqual('instantiated',v['learning']['status']);self.assertEqual(1,v['learning']['update_version'])
  self.assertEqual(self.c.learners['p'].binding.fingerprint,v['learning']['binding_fingerprint'])
  self.c.actual_updates=999;self.assertEqual(1,self.view()['learning']['update_version'])
 def test_cross_run_learning_binding_unavailable(self):
  begin(self.r,self.c);self.r.run_id='new';self.r.snapshot_id='new:1'
  self.assertEqual('not_attached',self.view()['learning']['status'])
 def test_last_affect_retains_its_old_evaluated_boundary_not_current_frame(self):
  role=self.p.crowd_role;engine=self.r.decision_engine
  engine.role_configs[role]=replace(engine.role_configs[role],capabilities={'emotion':True,'game':False,'learning':True})
  assembly=engine.assemblies[role];assembly.emotion_participates=True;assembly.desire=object()
  o=self.r.observations['p'];engine._boundary_contexts['p']=((o.snapshot_id,o.time_seconds),(PADState(),DesireState({'rest':.6},'rest'),self.p,self.s,o))
  engine.emotional_states['p']=PADState(.1,.2,.3,'qa')
  self.r.time_seconds=2;self.r.snapshot_id='r:2'
  v=self.view();self.assertEqual('evaluated',v['affect']['status']);self.assertEqual('r:1',v['affect']['evaluated_at']['snapshot_id']);self.assertEqual('r:2',v['snapshot_id'])
  self.assertEqual('rest',v['affect']['desire']['primary']);self.assertEqual(.1,v['affect']['pad']['pleasure'])
 def test_other_person_or_foreign_affect_boundary_never_displays_PAD(self):
  engine=self.r.decision_engine;role=self.p.crowd_role
  engine.role_configs[role]=replace(engine.role_configs[role],capabilities={'emotion':True,'game':False,'learning':True})
  engine.emotional_states['p']=PADState(.9)
  o=self.r.observations['p']
  for key,profile in [(('foreign:1',1),self.p),((o.snapshot_id,1),replace(self.p,person_id='other'))]:
   engine._boundary_contexts['p']=(key,(PADState(),DesireState({},'qa'),profile,self.s,o))
   self.assertIsNone(self.view()['affect']['pad'])
 def test_rejected_last_attempt_kept_distinct_from_previous_applied_plan(self):
  old=BehaviorPlan('p','r:0','continue',decided_at=0)
  self.s.current_plan=old
  attempt=BehaviorPlan('p','r:1','wait',decided_at=1,wait_until=3)
  receipt=PlanExecutionResult('p','wait','continue','rejected',1,'QA rejection')
  self.r.plan_executor.execution_evidence={'p':dict(snapshot_id='r:1',time_seconds=1,plan=asdict(attempt),receipt=asdict(receipt))}
  v=self.view();self.assertEqual('continue',v['current_plan']['proposed_action']);self.assertEqual('rejected',v['last_execution']['receipt']['status'])
  self.assertEqual('wait',v['last_execution']['plan']['proposed_action'])
 def test_foreign_execution_or_future_plan_not_presented(self):
  self.s.current_plan=BehaviorPlan('p','foreign:1','continue',decided_at=1)
  self.r.plan_executor.execution_evidence={'p':dict(snapshot_id='foreign:1',time_seconds=1,plan=asdict(self.s.current_plan),receipt=asdict(PlanExecutionResult('p','continue','continue','applied',1)))}
  self.assertIsNone(self.view()['last_execution']);self.assertIsNone(self.view()['current_plan'])
  self.s.current_plan=BehaviorPlan('p','r:4','continue',decided_at=4);self.assertIsNone(self.view()['current_plan'])
 def test_legacy_contract_does_not_add_or_guess_new_configuration(self):
  self.r.decision_engine.execution_contract=None;self.assertIsNone(self.view())
 def test_executor_receipt_freezes_attempt_and_is_pruned_with_person(self):
  executor=PlanExecutor(SimpleNamespace(set_person_speed=Mock()),Mock())
  plan=BehaviorPlan('p','r:1','continue',decided_at=1)
  executor.apply(plan,self.r.current.persons['p'],'r:1',1)
  self.assertEqual('applied',executor.execution_evidence['p']['receipt']['status'])
  self.assertEqual(asdict(plan),executor.execution_evidence['p']['plan'])
  executor.retain_active([]);self.assertEqual({},executor.execution_evidence)
 def test_null_last_update_does_not_crash_decision_trace_view(self):
  from crowdsim.infrastructure.decision_trace import DecisionTraceStore,VERSION
  self.r.decision_traces=DecisionTraceStore()
  self.r.time_seconds=2.0
  base=dict(version=VERSION,run_id=self.r.run_id,person_id='p',
   snapshot_id=f"{self.r.run_id}:1",time_seconds=1.0,
   plan={'proposed_action':'continue','target_id':None,'candidate_id':None},
   receipt={'status':'applied'},participation=[])
  base['learning']={'last_update':None}
  self.r.decision_traces.record(dict(base))
  v=self.view()
  self.assertIsNotNone(v)
  self.assertIsNone(v['decision_trace']['learning_acceptance'])
  base['learning']={'last_update':{'acceptance':True,'update_version':2}}
  self.r.decision_traces.record(dict(base))
  v=self.view()
  self.assertTrue(v['decision_trace']['learning_acceptance'])

if __name__=='__main__':unittest.main()
