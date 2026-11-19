"""Evaluated samples use actual interval/encoding contracts; weights are explicit QA."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import unittest
from crowdsim.domain.crowdsim_models import BehaviorPlan, PlanExecutionResult
from crowdsim.modeling_algorithm.individual_model.learning.reward_bridge import (
    RULE_ID, IntervalRewardRule, build_evaluated_sample)
from crowdsim.modeling_algorithm.individual_model.learning.observed_feedback import (
    ACTION_VERSION, FeatureEncoder, EncodedChoice, IntervalEvidence)
from crowdsim.modeling_algorithm.individual_model.learning.core_contracts import LearnerIdentity, canonical
from crowdsim.modeling_algorithm.individual_model.learning.learner_cores import create_learner_core
from tests.test_observed_feedback_stage_five import fixtures, point, journal, start, stage
from tests.test_learning_configuration_stage_five import config


def rule(weights=None,scales=None):
    weights=weights or {'risk_exposure':2.,'switch_cost':1.}
    return IntervalRewardRule(weights=weights,scales=scales or {k:1. for k in weights})


def setup(method='linucb_v1',reward=None):
    encoder=FeatureEncoder(columns=('bias','perceived_risk'),scales={'bias':1,'perceived_risk':1},
        state_bins={'perceived_risk':(.1,.5)},emotion_participates=False)
    r=reward or rule();c=config(method)
    c['parameters'].update(minimum_samples=1,update_every=1,update_budget=1)
    c['feature_schema_version']=encoder.fingerprint;c['action_schema_version']=ACTION_VERSION
    c['feedback']={'rule_id':RULE_ID,'version':r.fingerprint,'weights':r.spec['weights']}
    learner=create_learner_core(c,LearnerIdentity('r','sha256:qa','consumer','p'),feature_dimension=2,
        time_unit_seconds=1,bootstrap_on_truncation=False,storage_capacity=20)
    p,s,o=fixtures();plan=BehaviorPlan('p',o.snapshot_id,'continue',decided_at=1)
    before=encoder.encode(p,s,o,plan,());j=journal();start(j)
    end=point(2,risk=.4);j.advance('i',end,decision_boundary=True)
    row=j.batch()[0]
    p,s,o=fixtures(2);o=replace(o,perceived_risk=.4)
    nxt=encoder.encode(p,s,o,replace(plan,snapshot_id=o.snapshot_id,decided_at=2),())
    return learner,r,row,before,nxt


def alter(evidence,**changes):
    row=evidence.as_dict();row.update(changes)
    return IntervalEvidence(evidence.interval_id,canonical(row))


class RewardBridgeTests(unittest.TestCase):
    def test_reward_formula_and_detached_terms(self):
        c,r,e,b,n=setup();result=r.evaluate(e).as_dict()
        self.assertAlmostEqual(-.6,result['reward']) # 2 * negative integral .3
        self.assertTrue(result['trainable']);self.assertEqual(0,result['terms']['switch_cost']['raw'])
        result['terms']['risk_exposure']['raw']=999
        self.assertAlmostEqual(.3,r.evaluate(e).as_dict()['terms']['risk_exposure']['raw'])
        copy=r.spec;copy['weights'].clear();self.assertTrue(r.spec['weights'])

    def test_explicit_weights_scales_and_fingerprints(self):
        for weights,scales in [({},{}),({'unknown':1},{'unknown':1}),({'risk_exposure':True},{'risk_exposure':1}),
            ({'risk_exposure':1},{}),({'risk_exposure':0},{'risk_exposure':1}),({'risk_exposure':1},{'risk_exposure':0})]:
            with self.assertRaises(ValueError):IntervalRewardRule(weights=weights,scales=scales)
        self.assertNotEqual(rule().fingerprint,rule(scales={'risk_exposure':2,'switch_cost':1}).fingerprint)

    def test_unavailable_terms_block_not_impute(self):
        r=rule({'goal_progress':1,'risk_exposure':1})
        c,_,e,b,n=setup(reward=r);sample,res=build_evaluated_sample(c.binding,r,e,b,next_choices=(n,))
        self.assertIsNone(sample);self.assertIsNone(res.as_dict()['reward'])
        self.assertEqual(['goal_progress'],res.as_dict()['unavailable_terms'])
        r=rule({'unplanned_blocking':1});c,_,e,b,n=setup(reward=r)
        sample,res=build_evaluated_sample(c.binding,r,e,b,next_choices=(n,))
        self.assertIsNone(sample);self.assertEqual(0,c.updates)

    def test_disabled_missing_term_is_not_measured_zero(self):
        r=rule({'unplanned_blocking':0,'risk_exposure':1});c,_,e,b,n=setup(reward=r)
        sample,res=build_evaluated_sample(c.binding,r,e,b,next_choices=(n,))
        self.assertIsNotNone(sample);term=res.as_dict()['terms']['unplanned_blocking']
        self.assertIsNone(term['raw']);self.assertEqual('disabled',term['status'])

    def test_build_samples_update_linucb_and_q_without_mutating_evidence(self):
        for method in ('linucb_v1','tabular_q_v1'):
            c,r,e,b,n=setup(method);original=e.payload_json
            sample,res=build_evaluated_sample(c.binding,r,e,b,next_choices=(n,))
            self.assertIsNotNone(sample);self.assertAlmostEqual(-.6,sample.reward)
            self.assertEqual('accepted',c.observe(sample));updates=c.update(token='first')
            self.assertEqual(1,len(updates));self.assertNotEqual(updates[0].as_dict()['before'],updates[0].as_dict()['after'])
            self.assertEqual(original,e.payload_json);self.assertFalse(e.as_dict()['trainable'])
            self.assertEqual('duplicate',c.observe(sample));self.assertEqual((),c.update(token='first'))

    def test_ineligible_endings_never_become_samples(self):
        c,r,e,b,n=setup()
        for reason in ('timeline_end','external_control','unknown_disappearance','runtime_reset','sumo_arrived_without_stage'):
            modified=alter(e,end_reason=reason,truncated=reason!='sumo_arrived_without_stage',terminated=reason=='sumo_arrived_without_stage')
            sample,result=build_evaluated_sample(c.binding,r,modified,b,next_choices=(n,))
            self.assertIsNone(sample);self.assertFalse(result.as_dict()['trainable'])

    def test_identity_feedback_and_encoding_versions_reject(self):
        c,r,e,b,n=setup()
        for changed in (alter(e,model_fingerprint='other'),alter(e,run_id='another'),alter(e,role_id='another'),alter(e,person_id='q')):
            with self.assertRaises(ValueError):build_evaluated_sample(c.binding,r,changed,b,next_choices=(n,))
        other=rule(scales={'risk_exposure':2,'switch_cost':1})
        with self.assertRaises(ValueError):build_evaluated_sample(c.binding,other,e,b,next_choices=(n,))
        with self.assertRaises(ValueError):build_evaluated_sample(c.binding,r,e,replace(b,encoding_fingerprint='other'),next_choices=(n,))

    def test_receipt_proposal_actual_mismatch_or_corrupt_time_reject(self):
        c,r,e,b,n=setup()
        for action in ('wait','unknown'):
            row=e.as_dict();row['receipt']['applied_action']=action
            with self.assertRaises(ValueError):r.evaluate(IntervalEvidence('i',canonical(row)))
        for field,value in [('risk_exposure_seconds',True),('duration_seconds',2),('activity_seconds',{'moving':.5})]:
            with self.assertRaises(ValueError):r.evaluate(alter(e,**{field:value}))

    def test_next_choices_required_exact_boundary_and_unique(self):
        c,r,e,b,n=setup()
        for choices in ((),(n,n),(b,)):
            with self.assertRaises(ValueError):build_evaluated_sample(c.binding,r,e,b,next_choices=choices)
        c2,r2,e2,b2,n2=setup('bounded_adaptation_v1')
        sample,res=build_evaluated_sample(c2.binding,r2,e2,b2,next_choices=(n2,))
        self.assertIsNone(sample);self.assertEqual('completed_cost_required',res.as_dict()['reason'])

    def test_corrupt_raw_features_reject_and_next_order_does_not_change_sample_id(self):
        c,r,e,b,n=setup();raw=b.as_dict();raw['raw_values']['perceived_risk']=.8
        forged=replace(b,evidence_json=canonical(raw))
        with self.assertRaises(ValueError):build_evaluated_sample(c.binding,r,e,forged,next_choices=(n,))
        p,s,o=fixtures(2);o=replace(o,perceived_risk=.4)
        enc=FeatureEncoder(columns=('bias','perceived_risk'),scales={'bias':1,'perceived_risk':1},state_bins={'perceived_risk':(.1,.5)},emotion_participates=False)
        wait=enc.encode(p,s,o,BehaviorPlan('p',o.snapshot_id,'wait',decided_at=2,wait_until=4),())
        one,_=build_evaluated_sample(c.binding,r,e,b,next_choices=(n,wait))
        two,_=build_evaluated_sample(c.binding,r,e,b,next_choices=(wait,n))
        self.assertEqual(one,two)

    def test_incomplete_cost_refuses_bounded_update_even_if_reward_available(self):
        c,r,e,b,n=setup('bounded_adaptation_v1')
        self.assertIsNone(build_evaluated_sample(c.binding,r,e,b,next_choices=(n,))[0])
        with self.assertRaises(ValueError):build_evaluated_sample(c.binding,r,alter(e,cost_complete=True),b,next_choices=(n,))

    def test_zero_observed_reward_is_distinct_from_unavailable(self):
        r=rule({'risk_exposure':1});c,_,e,b,n=setup(reward=r)
        observed=alter(e,risk_exposure_seconds=0)
        sample,result=build_evaluated_sample(c.binding,r,observed,b,next_choices=(n,))
        self.assertEqual(0,sample.reward);self.assertTrue(result.as_dict()['trainable'])
        c2,_,e2,b2,n2=setup(reward=rule({'goal_progress':1}))
        self.assertIsNone(build_evaluated_sample(c2.binding,rule({'goal_progress':1}),e2,b2,next_choices=(n2,))[0])

    def test_corrupt_mode_missing_controls_or_empty_encoding_hard_reject(self):
        c,r,e,b,n=setup()
        for changes in ({'mode':'completed_route'},{'plan':{}},{'receipt':{}},{'duration_seconds':True}):
            with self.assertRaises(ValueError):r.evaluate(alter(e,**changes))
        row=e.as_dict();row['last']['constraints']={}
        with self.assertRaises(ValueError):r.evaluate(IntervalEvidence('i',canonical(row)))
        with self.assertRaises(ValueError):build_evaluated_sample(c.binding,r,e,replace(b,evidence_json='{}'),next_choices=(n,))

    def test_validated_rule_reward_recomputed_scale_and_route_write_count(self):
        r=rule({'risk_exposure':2},{'risk_exposure':3});c,_,e,b,n=setup(reward=r)
        self.assertAlmostEqual(-.2,r.evaluate(e).as_dict()['reward'])
        j=journal();start(j,mode='completed_route')
        j.advance('i',point(2,st=stage(kind=1,edges=('b',),desc='wait')),
            completed_stage=stage(depart=1,travel=1))
        reward=rule({'switch_cost':4},{'switch_cost':2}).evaluate(j.batch()[0]).as_dict()
        self.assertEqual(-2,reward['reward']);self.assertEqual(1,reward['terms']['switch_cost']['raw'])

    def test_conflicting_next_state_and_sample_identity_are_safe(self):
        c,r,e,b,n=setup()
        forged=replace(n,state=(0,))
        with self.assertRaises(ValueError):build_evaluated_sample(c.binding,r,e,b,next_choices=(n,forged))
        one,_=build_evaluated_sample(c.binding,r,e,b,next_choices=(n,))
        other,_=build_evaluated_sample(c.binding,r,alter(e,risk_exposure_seconds=.2),b,next_choices=(n,))
        self.assertEqual(one.sample_id,other.sample_id)
        self.assertEqual('accepted',c.observe(one));self.assertEqual('duplicate',c.observe(other))

    def test_completed_route_sample_and_tampered_completion_reject(self):
        from crowdsim.decision.route_provider import RouteCandidate,bind_candidates
        c,r,_,_,_=setup('bounded_adaptation_v1')
        encoder=FeatureEncoder(columns=('bias','perceived_risk'),scales={'bias':1,'perceived_risk':1},
            state_bins={'perceived_risk':(.1,.5)},emotion_participates=False)
        p,s,o=fixtures();cs=bind_candidates((RouteCandidate('goal','b',5,('a','b'),10),),'p',o.snapshot_id)
        plan=BehaviorPlan('p',o.snapshot_id,'reroute',decided_at=1,target_id='goal',route_edges=('a','b'),
            arrival_position=5,candidate_id=cs[0].candidate_id)
        before=encoder.encode(p,s,o,plan,cs);j=journal();start(j,mode='completed_route')
        wait_stage=stage(kind=1,edges=('b',),desc='wait')
        j.advance('i',point(2,risk=.2,st=wait_stage),completed_stage=stage(depart=1,travel=1))
        evidence=j.batch()[0];p,s,o=fixtures(2);o=replace(o,own_motion=replace(o.own_motion,stage_type=1))
        nxt=encoder.encode(p,s,o,BehaviorPlan('p',o.snapshot_id,'continue',decided_at=2),())
        sample,res=build_evaluated_sample(c.binding,r,evidence,before,next_choices=(nxt,))
        self.assertTrue(sample.cost_complete);self.assertEqual(1,sample.observed_cost_seconds)
        c.observe(sample);self.assertEqual(1,len(c.update(token='cost')))
        for field,value in [('travelTime',2),('description','forged')]:
            row=evidence.as_dict();row['completed_stage'][field]=value
            with self.assertRaises(ValueError):build_evaluated_sample(c.binding,r,IntervalEvidence('i',canonical(row)),before,next_choices=(nxt,))

    def test_frozen_and_replay_sample_updates_respect_counts(self):
        c,r,e,b,n=setup('tabular_q_v1')
        conf=c.binding.config;conf['replay']={'enabled':True,'capacity':2,'batch_size':1}
        replay=create_learner_core(conf,c.binding.identity,feature_dimension=2,time_unit_seconds=1,
            bootstrap_on_truncation=False,storage_capacity=20)
        sample,_=build_evaluated_sample(replay.binding,r,e,b,next_choices=(n,))
        replay.observe(sample);first=replay.update(token='one')
        self.assertTrue(first[0].as_dict()['replay']);self.assertEqual((),replay.update(token='one'))
        replay.freeze();before=replay.snapshot()
        self.assertEqual('frozen',replay.observe(sample));self.assertEqual((),replay.update(token='two'))
        self.assertEqual(before,replay.snapshot())

    def test_zero_length_interruption_reports_ineligible_without_fabricating_sample(self):
        j=journal();start(j);j.interrupt('i',reason='runtime_reset',now=1)
        result=rule().evaluate(j.batch()[0]).as_dict()
        self.assertIsNone(result['reward']);self.assertFalse(result['trainable'])
        self.assertEqual('ineligible_interval',result['reason'])

    def test_runtime_gate_stays_closed(self):
        from crowdsim.modeling_algorithm.individual_model.learning.configuration import require_learning_ready
        c,r,e,b,n=setup()
        with self.assertRaisesRegex(ValueError,'not implemented'):require_learning_ready(c.binding.config)
        self.assertFalse(c.diagnostics()['runtime_ready'])

if __name__=='__main__':unittest.main()
