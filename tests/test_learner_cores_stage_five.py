"""Closed-form core tests; all reward/features are explicit QA inputs, not production defaults."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch
import math
import numpy as np
from pathlib import Path
from crowdsim.modeling_algorithm.individual_model.learning.core_contracts import (
    LearnerIdentity, LearningSample, LegalChoice, semantic_route_key, choose_route_candidate)
from crowdsim.modeling_algorithm.individual_model.learning.learner_cores import create_learner_core
from tests.test_learning_configuration_stage_five import config
from crowdsim.modeling_algorithm.individual_model.learning.configuration import require_learning_ready
from crowdsim.decision.route_provider import RouteCandidate, bind_candidates

A=('reroute','goal','upper','qa-route-a')
B=('reroute','goal','lower','qa-route-b')
STATE=('risk-low','crowd-low')


def core(method='linucb_v1',*,capacity=100,dimension=1,bootstrap=False,params=None,subject='p',replay=None):
    c=config(method)
    c['parameters'].update(minimum_samples=1,update_every=1,update_budget=1)
    if method=='tabular_q_v1':c['parameters'].update(learning_rate=.5,discount_factor=.5,epsilon=0,table_capacity=100)
    if method=='bounded_adaptation_v1':c['parameters'].update(learning_rate=.5,forgetting_factor=1,correction_limit=100)
    if method=='linucb_v1':c['parameters'].update(regularization=1,exploration_coefficient=0)
    if params:c['parameters'].update(params)
    if replay is not None:c['replay']=replay
    return create_learner_core(c,LearnerIdentity('qa-run','sha256:qa','consumer',subject),
        feature_dimension=dimension,time_unit_seconds=1,bootstrap_on_truncation=bootstrap,storage_capacity=capacity)


def sample(c,*,name='one',action=A,reward=2,state=STATE,next_state=STATE,
           duration=1,terminated=False,truncated=False,next_actions=(A,B),features=(1.,),
           predicted=None,observed=None,complete=False):
    return LearningSample(name,c.binding.fingerprint,action,state,features,reward,duration,
        next_state,next_actions,terminated,truncated,'qa-feedback','1','qa-evidence:'+name,
        predicted,observed,complete)


class LearnerCoreTests(unittest.TestCase):
    def test_bounded_residual_formula_clip_and_explicit_completed_cost(self):
        c=core('bounded_adaptation_v1',params={'correction_limit':8})
        s=sample(c,predicted=10,observed=20,complete=True)
        c.observe(s);u=c.update(token='one')[0].as_dict()
        self.assertEqual(5,c.corrections[A]);self.assertEqual(10,u['detail']['residual_seconds'])
        c.observe(replace(s,sample_id='two',observed_cost_seconds=100));c.update(token='two')
        self.assertEqual(8,c.corrections[A])
        for bad in (sample(c),replace(s,cost_complete=False),replace(s,truncated=True)):
            with self.assertRaises(ValueError):c.observe(bad)
        self.assertNotEqual(u['before'],u['after'])

    def test_adaptation_changes_selected_legal_route_not_physics(self):
        c=core('bounded_adaptation_v1')
        choices=(LegalChoice(A,(1.,),10),LegalChoice(B,(1.,),12))
        self.assertEqual(A,c.choose(STATE,choices,token='before'))
        c.observe(sample(c,predicted=10,observed=20,complete=True));c.update(token='learn')
        self.assertEqual(B,c.choose(STATE,choices,token='after'))
        snap=c.snapshot();snap['state'].clear()
        self.assertTrue(c.snapshot()['state'])

    def test_linucb_closed_form_one_dimension_and_uncertainty(self):
        c=core(params={'exploration_coefficient':2})
        c.observe(sample(c,reward=4));c.update(token='one')
        # A=1+1=2, b=4, theta=2, UCB=2+2/sqrt(2).
        value=c.score(STATE,(LegalChoice(A,(1.,)),))[A]
        self.assertAlmostEqual(2+math.sqrt(2),value)
        model=c.snapshot()['state'][0]
        self.assertEqual([[2]],model['A']);self.assertEqual([4],model['b'])
        old=c.snapshot();c.score(STATE,(LegalChoice(B,(1.,)),))
        self.assertEqual(old,c.snapshot()) # scoring a new arm must not allocate/update it.

    def test_linucb_multidimensional_matrix_update_matches_independent_solve(self):
        c=core(dimension=2)
        x=(1.,2.);c.observe(sample(c,features=x,reward=3));c.update(token='one')
        matrix=np.eye(2)+np.outer(x,x);response=np.array(x)*3
        result=c.score(STATE,(LegalChoice(A,x),))[A]
        self.assertAlmostEqual(float(np.array(x)@np.linalg.solve(matrix,response)),result)

    def test_q_learning_formula_uses_next_legal_actions_and_duration(self):
        c=core('tabular_q_v1')
        next_state=('next',)
        c.observe(sample(c,name='seed',state=next_state,action=B,reward=10,terminated=True,next_actions=()))
        c.update(token='seed') # Q(next,B)=5
        c.observe(sample(c,state=STATE,next_state=next_state,reward=2,duration=2,next_actions=(B,)))
        update=c.update(token='two')[0].as_dict()
        self.assertAlmostEqual(1.625,c.values[(STATE,A)]) # .5*(2 + .5^2*5)
        self.assertEqual(.25,update['detail']['effective_discount'])
        self.assertTrue(update['detail']['bootstrap'])

    def test_q_terminal_and_truncation_bootstrap_are_not_conflated(self):
        for bootstrap in (False,True):
            c=core('tabular_q_v1',bootstrap=bootstrap)
            c.observe(sample(c,name='seed',action=B,reward=10,terminated=True,next_actions=()));c.update(token='seed')
            c.observe(sample(c,reward=2,truncated=True,next_actions=(B,)));c.update(token='next')
            self.assertAlmostEqual(2.25 if bootstrap else 1,c.values[(STATE,A)])
            terminal=sample(c,name='terminal',action=A,reward=2,terminated=True,next_actions=())
            c.observe(terminal);u=c.update(token='terminal')[0].as_dict()
            self.assertFalse(u['detail']['bootstrap'])

    def test_scope_binding_feedback_finite_and_mutable_input_reject_without_update(self):
        c=core();s=sample(c)
        bads=[{},replace(s,reward=True),replace(s,reward=float('nan')),replace(s,features=(float('inf'),)),
              replace(s,features=(1.,2.)),replace(s,action=('reroute','candidate-abc')),
              replace(s,next_actions=(['continue'],)),replace(s,binding_fingerprint='other'),
              replace(s,duration_seconds=0),replace(s,feedback_version='2'),replace(s,state=[1]),
              replace(s,terminated=True,truncated=True)]
        for bad in bads:
            with self.subTest(bad=bad),self.assertRaises(ValueError):c.observe(bad)
        self.assertEqual(0,c.accepted);self.assertEqual(0,c.updates)
        with self.assertRaises(FrozenInstanceError):s.reward=9

    def test_minimum_sample_gate_period_budget_and_tokens(self):
        c=core(params={'minimum_samples':2,'update_every':2,'update_budget':1})
        c.observe(sample(c));self.assertEqual((),c.update(token='early'))
        c.observe(sample(c,name='two'));self.assertEqual(1,len(c.update(token='boundary')))
        self.assertEqual((),c.update(token='boundary'))
        self.assertEqual(1,len(c.samples));self.assertEqual(1,len(c.update(token='another')))
        self.assertEqual(2,c.updates)

    def test_duplicate_ids_frozen_and_instance_isolation(self):
        c=core();d=core(subject='other');s=sample(c)
        self.assertEqual('accepted',c.observe(s));self.assertEqual('duplicate',c.observe(s))
        c.update(token='one');self.assertEqual([],d.snapshot()['state'])
        with self.assertRaises(ValueError):d.observe(s)
        config_copy=c.binding.config;config_copy['feedback']['version']='changed'
        self.assertEqual('1',c.binding.config['feedback']['version'])
        c.freeze();before=c.snapshot()
        self.assertEqual('frozen',c.observe(sample(c,name='next')));self.assertEqual((),c.update(token='frozen'))
        self.assertEqual(before,c.snapshot())

    def test_replay_deterministic_sampling_duplicate_tokens_and_no_double_append(self):
        replay={'enabled':True,'capacity':3,'batch_size':2}
        c=core('tabular_q_v1',params={'update_budget':2},replay=replay)
        d=core('tabular_q_v1',params={'update_budget':2},replay=replay)
        for i in range(4):
            for v in (c,d):v.observe(sample(v,name=str(i),reward=i))
        self.assertEqual(3,len(c.samples));self.assertEqual('duplicate',c.observe(sample(c,name='0',reward=0)))
        u=c.update(token='same');v=d.update(token='same')
        self.assertEqual([x.as_dict() for x in u],[x.as_dict() for x in v])
        self.assertEqual((),c.update(token='same'));c.update(token='other')
        self.assertEqual((),c.update(token='same'));self.assertEqual(3,len(c.samples))

    def test_q_capacity_failure_rolls_back_entire_update_batch(self):
        c=core('tabular_q_v1',params={'table_capacity':1,'update_budget':2})
        c.observe(sample(c,action=A));c.observe(sample(c,name='two',action=B))
        before=c.snapshot()
        with self.assertRaises(BufferError):c.update(token='fail')
        self.assertEqual(before,c.snapshot());self.assertEqual(2,len(c.samples))
        self.assertEqual(0,c.updates)

    def test_core_does_not_open_runtime_gate_or_silently_support_role_scope(self):
        c=core()
        with self.assertRaisesRegex(ValueError,'not implemented'):require_learning_ready(c.binding.config)
        conf=c.binding.config;conf['policy_scope']='role'
        with self.assertRaisesRegex(ValueError,'individual'):
            create_learner_core(conf,c.binding.identity,feature_dimension=1,time_unit_seconds=1,
                bootstrap_on_truncation=False,storage_capacity=100)
        self.assertFalse(c.diagnostics()['runtime_ready'])

    def test_semantic_route_identity_reuses_across_snapshots_but_not_itinerary(self):
        raw=(RouteCandidate('goal','end',10,('start','upper','end'),10,entry_edge='upper'),
             RouteCandidate('goal','end',10,('start','lower','end'),12,entry_edge='lower'))
        a=bind_candidates(raw,'p','one');b=bind_candidates(tuple(replace(x,estimated_cost_seconds=20) for x in raw),'p','two')
        self.assertNotEqual(a[0].candidate_id,b[0].candidate_id)
        self.assertEqual(semantic_route_key('reroute',a[0]),semantic_route_key('reroute',b[0]))
        self.assertNotEqual(semantic_route_key('reroute',a[0]),semantic_route_key('reroute',a[1]))
        c=core('bounded_adaptation_v1')
        selected=choose_route_candidate(c,STATE,a,((1.,),(1.,)),action='reroute',token='before')
        self.assertEqual(a[0],selected)
        c.observe(sample(c,action=semantic_route_key('reroute',a[0]),predicted=10,observed=30,complete=True));c.update(token='learn')
        selected=choose_route_candidate(c,STATE,a,((1.,),(1.,)),action='reroute',token='after')
        self.assertEqual(a[1],selected)

    def test_in_memory_checkpoints_resume_exactly_and_freeze_without_updates(self):
        for method in ('bounded_adaptation_v1','linucb_v1','tabular_q_v1'):
            c=core(method)
            s=sample(c,predicted=10,observed=20,complete=True)
            c.observe(s);c.update(token='first')
            c.observe(replace(s,sample_id='pending'))
            checkpoint=c.export_checkpoint();d=core(method)
            d.load_checkpoint(checkpoint)
            self.assertEqual(c.export_checkpoint(),d.export_checkpoint())
            c.update(token='second');d.update(token='second')
            self.assertEqual(c.snapshot(),d.snapshot())
            d.freeze();before=d.snapshot()
            self.assertEqual('frozen',d.observe(replace(s,sample_id='freeze')))
            self.assertEqual((),d.update(token='freeze'));self.assertEqual(before,d.snapshot())

    def test_checkpoint_binding_tamper_and_invalid_matrix_are_atomic(self):
        from crowdsim.modeling_algorithm.individual_model.learning.core_contracts import digest
        c=core();c.observe(sample(c));c.update(token='one');checkpoint=c.export_checkpoint()
        d=core();before=d.export_checkpoint()
        bad=deepcopy(checkpoint);bad['state'][0]['A']=[[-1.]]
        bad['checksum']=digest({k:v for k,v in bad.items() if k!='checksum'})
        bad_numeric=deepcopy(checkpoint);bad_numeric['state'][0]['b']=[True]
        bad_numeric['checksum']=digest({k:v for k,v in bad_numeric.items() if k!='checksum'})
        for value in (dict(checkpoint,checksum='forged'),bad,bad_numeric,dict(checkpoint,extra=True)):
            with self.assertRaises(ValueError):d.load_checkpoint(value)
            self.assertEqual(before,d.export_checkpoint())
        other=core(subject='another')
        with self.assertRaises(ValueError):other.load_checkpoint(checkpoint)

    def test_scoring_targets_tie_order_and_sample_capacity(self):
        c=core(capacity=1);c.observe(sample(c))
        with self.assertRaises(BufferError):c.observe(sample(c,name='two'))
        choices=(LegalChoice(B,(1.,)),LegalChoice(A,(1.,)))
        self.assertEqual(c.choose(STATE,choices,token='tie'),c.choose(STATE,tuple(reversed(choices)),token='tie'))
        conf=c.binding.config;conf['targets']=['action']
        action_core=create_learner_core(conf,c.binding.identity,feature_dimension=1,time_unit_seconds=1,
            bootstrap_on_truncation=False,storage_capacity=1)
        with self.assertRaisesRegex(ValueError,'targets'):action_core.observe(sample(action_core))
        with self.assertRaisesRegex(ValueError,'targets'):action_core.choose(STATE,choices,token='route')

    def test_linucb_failed_second_update_rolls_back_first_and_queue(self):
        c=core(params={'update_budget':2});c.observe(sample(c));c.observe(sample(c,name='second',action=B))
        before=c.export_checkpoint();original=c._learn
        def fail_second(s):
            if s.sample_id=='second':raise ValueError('injected update failure')
            return original(s)
        with patch.object(c,'_learn',side_effect=fail_second):
            with self.assertRaisesRegex(ValueError,'injected'):c.update(token='failed')
        self.assertEqual(before,c.export_checkpoint())

    def test_integer_valued_json_numbers_and_frozen_q_greedy_evaluation(self):
        c=core('tabular_q_v1',params={'minimum_samples':1.,'update_every':1.,
            'update_budget':1.,'table_capacity':10.,'epsilon':1.},
            replay={'enabled':True,'capacity':3.,'batch_size':1.})
        c.observe(sample(c,action=B,reward=8,terminated=True,next_actions=()))
        u=c.update(token='first')[0].as_dict()
        self.assertEqual(8,u['feedback']['reward']);self.assertTrue(u['replay'])
        self.assertEqual('first',u['update_token'])
        # The update record carries the consumed sample's immutable payload, not only an id/digest.
        self.assertEqual(list(B),u['sample']['action'])
        self.assertEqual(8,u['sample']['reward'])
        self.assertEqual(list(STATE),u['sample']['state'])
        self.assertTrue(u['sample']['terminated'])
        self.assertEqual([],u['sample']['next_actions'])
        choices=(LegalChoice(A,(1.,)),LegalChoice(B,(1.,)))
        d=core('tabular_q_v1',params={'minimum_samples':1.,'update_every':1.,
            'update_budget':1.,'table_capacity':10.,'epsilon':1.},
            replay={'enabled':True,'capacity':3.,'batch_size':1.})
        d.load_checkpoint(c.export_checkpoint())
        self.assertEqual((),d.update(token='first'))
        d.freeze()
        self.assertTrue(all(d.choose(STATE,choices,token=str(i))==B for i in range(30)))
        self.assertEqual(c.snapshot()['state'],d.snapshot()['state'])

    def test_checkpoint_versions_and_malformed_vectors_reject_atomically(self):
        from crowdsim.modeling_algorithm.individual_model.learning.core_contracts import digest
        c=core();c.observe(sample(c));checkpoint=c.export_checkpoint();d=core()
        before=d.export_checkpoint()
        for field,value in [('features',[True]),('next_actions',[['unknown']]),('reward',None)]:
            bad=deepcopy(checkpoint);bad['samples'][0][field]=value
            bad['checksum']=digest({k:v for k,v in bad.items() if k!='checksum'})
            with self.assertRaises(ValueError):d.load_checkpoint(bad)
            self.assertEqual(before,d.export_checkpoint())
        for field in ('feature_schema_version','action_schema_version'):
            conf=c.binding.config;conf[field]='new'
            other=create_learner_core(conf,c.binding.identity,feature_dimension=1,
                time_unit_seconds=1,bootstrap_on_truncation=False,storage_capacity=100)
            with self.assertRaisesRegex(ValueError,'binding'):other.load_checkpoint(checkpoint)

    def test_bounded_forgetting_lower_clip_and_nonroute_choices_reject(self):
        c=core('bounded_adaptation_v1',params={'forgetting_factor':.5,'correction_limit':8})
        c.observe(sample(c,predicted=10,observed=20,complete=True));c.update(token='first')
        c.observe(sample(c,name='second',predicted=10,observed=10,complete=True));c.update(token='second')
        self.assertEqual(1.25,c.corrections[A])
        c.observe(sample(c,name='third',predicted=100,observed=0,complete=True));c.update(token='third')
        self.assertEqual(-8,c.corrections[A])
        with self.assertRaises(ValueError):c.choose(STATE,(LegalChoice(('wait',),(1.,),1),),token='bad')

    def test_learning_scores_only_current_legal_arms_and_deterministic_q_exploration(self):
        for method in ('linucb_v1','tabular_q_v1'):
            c=core(method)
            c.observe(sample(c,action=B,reward=20,terminated=True,next_actions=()));c.update(token='one')
            only=(LegalChoice(A,(1.,)),)
            self.assertEqual(A,c.choose(STATE,only,token='only-legal'))
        c=core('tabular_q_v1',params={'epsilon':1})
        choices=(LegalChoice(A,(1.,)),LegalChoice(B,(1.,)))
        found={c.choose(STATE,choices,token=str(i)) for i in range(30)}
        self.assertEqual({A,B},found)
        self.assertEqual(c.choose(STATE,choices,token='repeat'),
            c.choose(STATE,tuple(reversed(choices)),token='repeat'))

    def test_route_adapter_rejects_missing_duplicate_identity_or_semantics(self):
        raw=(RouteCandidate('goal','end',10,('start','upper','end'),10,entry_edge='upper'),)
        cs=bind_candidates(raw,'p','one');c=core()
        with self.assertRaises(ValueError):choose_route_candidate(c,STATE,raw,((1.,),),action='reroute',token='bad')
        with self.assertRaises(ValueError):choose_route_candidate(c,STATE,cs,(),action='reroute',token='bad')
        with self.assertRaises(ValueError):choose_route_candidate(c,STATE,(cs[0],replace(cs[0],candidate_id='candidate-other')),((1.,),(1.,)),action='reroute',token='bad')

    def test_explicit_route_action_does_not_infer_from_target_kind(self):
        raw=(RouteCandidate('goal','end',10,('start','end'),10,target_kind='activity',
             activity_duration=20,next_route_edges=('end','exit'),next_target_id='exit'),)
        cs=bind_candidates(raw,'p','one');c=core()
        for action in ('reroute','change_goal'):
            self.assertEqual(cs[0],choose_route_candidate(c,STATE,cs,((1.,),),action=action,token='one'))
        self.assertNotEqual(semantic_route_key('reroute',cs[0]),semantic_route_key('change_goal',cs[0]))
        with self.assertRaises(ValueError):choose_route_candidate(c,STATE,cs,((1.,),),action='wait',token='one')

    def test_real_sumo_learner_selected_second_route_is_executed(self):
        from crowdsim.infrastructure.sumo_adapter import SumoAdapter
        from crowdsim.infrastructure.network_adapter import ResearchNetwork
        from crowdsim.decision.route_provider import RouteProvider
        from crowdsim.decision.plan_executor import PlanExecutor
        from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
        from crowdsim.domain.crowdsim_models import AgentProfile, Observation
        root=Path(__file__).resolve().parents[1]
        for method in ('bounded_adaptation_v1','linucb_v1','tabular_q_v1'):
            with self.subTest(method=method):
                adapter=SumoAdapter(root/'tests/scenarios/alternative_routes/scenario.sumocfg',sumo_binary=str(root/'.venv/sumo-1.24.0/bin/sumo'))
                try:
                    adapter.start();step=adapter.step();m=next(iter(step.persons.values()))
                    provider=RouteProvider(ResearchNetwork(root/'tests/scenarios/alternative_routes/network.net.xml'),adapter)
                    stages=adapter.remaining_person_stages(m.person_id);arrival=float(stages[0].arrivalPos)
                    raw=[provider.build_via_candidate(m,target_id='goal',target_edge='exit',via_edge=v,arrival_position=arrival) for v in ('upper_a','lower_a')]
                    if method=='bounded_adaptation_v1':
                        raw=[replace(candidate,estimated_cost_seconds=10+2*i) for i,candidate in enumerate(raw)]
                    cs=bind_candidates(raw,m.person_id,'qa-snapshot');c=core(method)
                    if method!='bounded_adaptation_v1':
                        c.observe(sample(c,name='upper-prior',action=semantic_route_key('reroute',cs[0]),
                            reward=2,terminated=True,next_actions=()))
                        c.update(token='upper-prior')
                    self.assertEqual(cs[0],choose_route_candidate(c,STATE,cs,((1.,),(1.,)),
                        action='reroute',token='before'))
                    # Explicit controlled QA feedback, not a measured behavioural reward.
                    if method=='bounded_adaptation_v1':
                        key=semantic_route_key('reroute',cs[0])
                        c.observe(sample(c,action=key,predicted=cs[0].estimated_cost_seconds,
                            observed=cs[0].estimated_cost_seconds+100,complete=True))
                    else:
                        c.observe(sample(c,action=semantic_route_key('reroute',cs[1]),reward=10))
                    c.update(token='qa-update')
                    selected=choose_route_candidate(c,STATE,cs,((1.,),(1.,)),action='reroute',token='qa-choice')
                    self.assertEqual(cs[1],selected)
                    o=Observation(m.person_id,'qa-snapshot',step.time_seconds,m)
                    plan=LLMDrivenDecisionModel._make_plan(AgentProfile(m.person_id),o,'reroute',selected,reason='QA learner core',confidence=None,source='qa_learner')
                    result=PlanExecutor(adapter,provider).apply(plan,m,'qa-snapshot',step.time_seconds,candidates=cs,require_candidate=True)
                    self.assertEqual('applied',result.status)
                    after=adapter.remaining_person_stages(m.person_id)
                    self.assertIn('lower_a',after[0].edges);self.assertNotIn('upper_a',after[0].edges)
                    self.assertEqual(selected.edges,tuple(after[0].edges));self.assertEqual(arrival,float(after[0].arrivalPos))
                    self.assertEqual(stages[1:],after[1:])
                finally:
                    adapter.close()
                self.assertTrue(adapter.closed)

class LearnerCoreEvidenceProbeTests(unittest.TestCase):
    def test_linucb_update_reports_old_and_new_theta_and_b(self):
        c = core('linucb_v1', dimension=1)
        c.observe(sample(c, name='s1', features=(1.,)))
        detail = c.update(token='t1')[0].as_dict()['detail']
        self.assertIn('theta', detail)
        self.assertIn('previous_b', detail)
        self.assertIn('new_b', detail)
        self.assertIsNone(detail['previous_theta'])
        c.observe(sample(c, name='s2', features=(1.,)))
        detail2 = c.update(token='t2')[0].as_dict()['detail']
        self.assertIsNotNone(detail2['previous_theta'])
        self.assertEqual(len(detail2['previous_theta']), 1)

    def test_sample_carries_target_and_candidate_identity(self):
        c = core('linucb_v1')
        s = LearningSample('x', c.binding.fingerprint, A, STATE, (1.,), 1, 1, STATE, (A,),
                           False, False, 'r', 'v', 'e', None, None, False, 'goal', 'candidate-x')
        self.assertEqual('goal', s.target_id)
        self.assertEqual('candidate-x', s.candidate_id)
        default = LearningSample('y', c.binding.fingerprint, A, STATE, (1.,), 1, 1, STATE, (A,),
                                 False, False, 'r', 'v', 'e')
        self.assertIsNone(default.target_id)
        self.assertIsNone(default.candidate_id)


if __name__=='__main__':unittest.main()
