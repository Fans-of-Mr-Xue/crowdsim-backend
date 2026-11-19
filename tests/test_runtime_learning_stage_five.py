"""Explicit controller Runtime hooks: isolation, causal selection, no fake feedback."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from crowdsim.core.simulation_runtime import SimulationRuntime,RuntimeState
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
from crowdsim.domain.crowdsim_models import BehaviorPlan,PlanExecutionResult
from crowdsim.modeling_algorithm.individual_model.learning.runtime_controller import RoleLearningController
from crowdsim.modeling_algorithm.individual_model.learning.observed_feedback import ACTION_VERSION,FeatureEncoder
from crowdsim.modeling_algorithm.individual_model.learning.reward_bridge import RULE_ID,IntervalRewardRule
from crowdsim.modeling_algorithm.individual_model.pipeline import IndividualModelingEngine
from tests.test_learning_configuration_stage_five import config
from tests.test_observed_feedback_stage_five import fixtures,stage,HOLDS
from tests.test_modeling_binding_stage_one import compiled_model


def settings(method='linucb_v1',mode='decision_interval',integration='baseline_route_refinement'):
    enc=dict(columns=['bias','perceived_risk'],scales={'bias':1,'perceived_risk':1},
        state_bins={'perceived_risk':[.1,.5]},emotion_participates=False)
    encoder=FeatureEncoder(columns=tuple(enc['columns']),scales=enc['scales'],
        state_bins={k:tuple(v) for k,v in enc['state_bins'].items()},emotion_participates=False)
    reward=IntervalRewardRule(weights={'risk_exposure':1,'switch_cost':10},scales={'risk_exposure':1,'switch_cost':1})
    c=config(method);c['parameters'].update(minimum_samples=1,update_every=1,update_budget=1)
    if method=='tabular_q_v1':c['parameters'].update(table_capacity=100,epsilon=0)
    c['feature_schema_version']=encoder.fingerprint;c['action_schema_version']=ACTION_VERSION
    c['feedback']={'rule_id':RULE_ID,'version':reward.fingerprint,'weights':reward.spec['weights']}
    if mode=='completed_route':c['targets']=['goal_route']
    return dict(learning_config=c,encoding=enc,reward_scales=reward.spec['scales'],interval_mode=mode,
        integration_mode=integration,storage_capacity=100,bootstrap_on_truncation=False)


def runtime_stub(method='linucb_v1',cap=100):
    p,s,o=fixtures()
    e=IndividualModelingEngine();e.configure(compiled_model()['individual_model']['agent_types'],execution_contract='candidate-decision-v1')
    r=SimpleNamespace(decision_engine=e,use_llm=False,run_id='r',step_length=1,visual_policy=SimpleNamespace(low_speed_threshold_mps=.2),
        population=SimpleNamespace(profile_for=lambda pid:p if pid=='p' else replace(p,person_id=pid),
            states={'p':s},locked_itinerary_ids=set(),ledger=SimpleNamespace(unknown_disappearances=set(),explicitly_removed={},arrived_ids=set())),
        observations={'p':o},current=SimpleNamespace(persons={'p':o.own_motion}),
        snapshot_id=o.snapshot_id,time_seconds=o.time_seconds,decision_candidates={'p':()},
        adapter=SimpleNamespace(current_person_stage=lambda pid:SimpleNamespace(**stage().as_dict())),
        plan_executor=SimpleNamespace(validate_plan=Mock()),last_error='')
    r._post_action_constraints=lambda pid:deepcopy(HOLDS)
    c=RoleLearningController({'consumer':settings(method)},capacity=cap);c.attach(r,model_fingerprint='sha256:qa')
    return r,c


def next_step(r,now,risk=.4):
    p,s,o=fixtures(now);o=replace(o,perceived_risk=risk)
    r.time_seconds=now;r.snapshot_id=o.snapshot_id;r.current.persons={'p':o.own_motion}
    r.observations={'p':o};r.population.states={'p':s}


def local(r):return BehaviorPlan('p',r.snapshot_id,'continue',decided_at=r.time_seconds,scheduling_source='configured_rule')


def begin(r,c):
    plan=c.refine(r,local(r));prepared=c.prepare(r,plan)
    c.accept(r,prepared,PlanExecutionResult('p','continue','continue','applied',r.time_seconds))


class RuntimeControllerTests(unittest.TestCase):
    def test_schema_rejects_missing_fields_versions_and_unsupported_choices(self):
        for changed in ('missing','schema','mode','sharing','inherit','attribution'):
            s=settings()
            if changed=='missing':s.pop('reward_scales')
            if changed=='schema':s['learning_config']['feature_schema_version']='wrong'
            if changed=='mode':s['integration_mode']='overwrite_game'
            if changed=='sharing':s['learning_config']['policy_scope']='role'
            if changed=='inherit':s['learning_config']['lifecycle']={'mode':'inherit','checkpoint_ref':'qa','cohort_key':'qa'}
            if changed=='attribution':s['learning_config']['feedback']['weights']['goal_progress']=1
            with self.subTest(changed=changed),self.assertRaises(ValueError):RoleLearningController({'consumer':s},capacity=100)
        with self.assertRaises(ValueError):RoleLearningController({'consumer':settings()},capacity=1)

    def test_detached_configuration_and_role_method_choices(self):
        original={'consumer':settings('tabular_q_v1'),'social_visitor':settings('linucb_v1')}
        c=RoleLearningController(original,capacity=100);fp=c.fingerprint
        original['consumer']['learning_config']['parameters']['epsilon']=1
        self.assertEqual(0,c.roles['consumer']['setting']['learning_config']['parameters']['epsilon'])
        self.assertEqual(fp,c.fingerprint);self.assertFalse(c.diagnostics()['product_contract_ready'])

    def test_one_real_boundary_sample_update_and_no_preaction_reward(self):
        r,c=runtime_stub();begin(r,c)
        self.assertEqual(0,c.actual_updates)
        next_step(r,2);c.advance(r,timeline_end=False)
        self.assertEqual(0,c.actual_updates)
        c.refine(r,local(r))
        self.assertEqual(1,c.accepted_samples);self.assertEqual(1,c.actual_updates)
        rows=[json.loads(x) for x in c.batch()]
        update=next(x for x in rows if x['kind']=='learning_update')
        self.assertAlmostEqual(-.3,update['sample']['reward'])
        self.assertEqual('continue',update['sample']['action'][0])
        self.assertEqual(2,update['next_encodings'][0]['time_seconds'])
        self.assertFalse(next(x for x in rows if x['kind']=='interval')['evidence']['trainable'])
        c.refine(r,local(r));self.assertEqual(1,c.actual_updates)

    def test_individual_scope_same_role_never_shares_model_state(self):
        r,c=runtime_stub('tabular_q_v1');begin(r,c);next_step(r,2);c.advance(r,timeline_end=False);c.refine(r,local(r))
        other=c._person(r,'other');self.assertEqual(0,other.accepted)
        self.assertNotEqual(other.binding.fingerprint,c.learners['p'].binding.fingerprint)
        self.assertIsNot(other,c.learners['p'])

    def test_rejected_receipt_does_not_start_interval(self):
        r,c=runtime_stub();prepared=c.prepare(r,local(r))
        for status in ('rejected','partial_failure'):
            c.accept(r,prepared,PlanExecutionResult('p','continue',None,status,1))
        self.assertFalse(c.contexts);self.assertEqual(0,c.actual_updates)
        self.assertEqual(2,len(c.batch()))

    def test_time_truncation_has_no_reward_update(self):
        r,c=runtime_stub();begin(r,c);next_step(r,2);c.advance(r,timeline_end=True)
        self.assertEqual(0,c.actual_updates);self.assertFalse(c.contexts)
        evidence=next(json.loads(x)['evidence'] for x in c.batch() if json.loads(x)['kind']=='interval')
        self.assertTrue(evidence['truncated']);self.assertIsNone(evidence['reward'])

    def test_external_limits_interrupt_and_never_start_training(self):
        r,c=runtime_stub();begin(r,c);next_step(r,2)
        r._post_action_constraints=lambda pid:{**deepcopy(HOLDS),'control_limits':{'qa':0}}
        c.advance(r,timeline_end=False);self.assertEqual(0,c.actual_updates)
        self.assertIsNone(c.prepare(r,local(r)))
        self.assertFalse(c.journal.pending)

    def test_runtime_activity_is_archived_but_not_trained(self):
        r,c=runtime_stub();begin(r,c)
        prepared=c.prepare(r,replace(local(r),scheduling_source='runtime_activity'))
        self.assertIsNone(prepared);self.assertFalse(c.contexts);self.assertEqual(0,c.accepted_samples)

    def test_disappearance_terminal_has_no_fabricated_next_state(self):
        r,c=runtime_stub();begin(r,c);r.current.persons={};r.time_seconds=2;r.population.ledger.arrived_ids.add('p')
        c.advance(r,timeline_end=False)
        self.assertEqual(0,c.accepted_samples)
        e=next(json.loads(x)['evidence'] for x in c.batch() if json.loads(x)['kind']=='interval')
        self.assertTrue(e['terminated']);self.assertEqual(1,e['unobserved_tail_seconds'])

    def test_old_run_and_attach_llm_capability_reject(self):
        r,c=runtime_stub();r.run_id='new'
        with self.assertRaises(ValueError):c.refine(r,local(r))
        r,c=runtime_stub();c2=RoleLearningController({'consumer':settings()},capacity=100)
        r.use_llm=True
        with self.assertRaises(ValueError):c2.attach(r,model_fingerprint='qa')
        self.assertIsNone(c2.run_id)

    def test_close_is_idempotent_and_ack_is_ordered(self):
        r,c=runtime_stub();begin(r,c);c.close(r);batch=c.batch();c.close(r)
        self.assertEqual(batch,c.batch());self.assertTrue(c.closed)
        with self.assertRaises(ValueError):c.acknowledge(('wrong',))
        c.acknowledge(batch);self.assertFalse(c.batch())

    def test_update_failure_restores_sample_and_policy_and_retains_interval(self):
        r,c=runtime_stub();begin(r,c);next_step(r,2);c.advance(r,timeline_end=False)
        l=c.learners['p'];before=l.export_checkpoint()
        with patch.object(l,'update',side_effect=BufferError('QA update fail')):
            with self.assertRaises(BufferError):c.refine(r,local(r))
        self.assertEqual(before,l.export_checkpoint());self.assertEqual(0,c.actual_updates)
        self.assertIn('finished',c.contexts['p'])
        c.refine(r,local(r));self.assertEqual(1,c.actual_updates)

    def test_adapter_attachment_only_ready_and_atomic_manifest(self):
        r,c=runtime_stub();r.state=RuntimeState.RUNNING;r._async_tick_inflight=None;r.learning_controller=None
        with self.assertRaises(RuntimeError):SimulationRuntime.attach_learning_controller(r,{'consumer':settings()},capacity=100)
        r.state=RuntimeState.READY;r.class2_flows=None;r.artificial_society_record={'fingerprint':'sha256:qa'}
        r.recorder=SimpleNamespace(update_manifest=Mock(side_effect=OSError('QA manifest fail')),
                                   record_runtime_observations=Mock())
        with self.assertRaises(OSError):SimulationRuntime.attach_learning_controller(r,{'consumer':settings()},capacity=100)
        self.assertIsNone(r.learning_controller)
        r.recorder.update_manifest=Mock()
        r.recorder.record_runtime_observations=Mock(side_effect=OSError('QA source archive fail'))
        with self.assertRaises(OSError):SimulationRuntime.attach_learning_controller(r,{'consumer':settings()},capacity=100)
        self.assertIsNone(r.learning_controller)
        r.recorder.record_runtime_observations=Mock()
        SimulationRuntime.attach_learning_controller(r,{'consumer':settings()},capacity=100)
        r.recorder.record_runtime_observations.assert_called_once_with(r)
        self.assertIsNotNone(r.learning_controller)
        with self.assertRaises(RuntimeError):SimulationRuntime.attach_learning_controller(r,{'consumer':settings()},capacity=100)

    def test_runtime_flush_failure_retains_unacknowledged_batch(self):
        r,c=runtime_stub();c._emit('QA');r.learning_controller=c
        r.recorder=SimpleNamespace(record_learning_runtime=Mock(side_effect=OSError('QA write fail')))
        with self.assertRaises(OSError):SimulationRuntime._flush_learning(r)
        self.assertEqual(1,len(c.batch()))
        r.recorder.record_learning_runtime=Mock();SimulationRuntime._flush_learning(r)
        self.assertEqual(0,len(c.batch()))

    def test_new_run_controller_reset_has_no_inherited_policy(self):
        r,c=runtime_stub();begin(r,c);next_step(r,2);c.advance(r,timeline_end=False);c.refine(r,local(r));c.close(r)
        new=RoleLearningController(json.loads(c.settings_json),capacity=100);r.run_id='new'
        new.attach(r,model_fingerprint='sha256:qa');self.assertFalse(new.learners)
        self.assertEqual(0,new.actual_updates);self.assertNotEqual(new.run_id,c.run_id)

    def test_bounded_cost_method_only_full_route_supported(self):
        with self.assertRaises(ValueError):RoleLearningController({'consumer':settings('bounded_adaptation_v1')},capacity=100)
        c=RoleLearningController({'consumer':settings('bounded_adaptation_v1','completed_route')},capacity=100)
        self.assertEqual('completed_route',c.diagnostics()['roles']['consumer']['interval_mode'])

    def test_locked_itinerary_baseline_is_never_replaced(self):
        from tests.test_candidate_decision_stage_four import candidates
        r,c=runtime_stub();r.population.locked_itinerary_ids.add('p');r.decision_candidates={'p':candidates('p',r.snapshot_id)}
        base=local(r);self.assertEqual(base,c.refine(r,base))

    def test_audit_capacity_fails_before_physical_execution(self):
        r,c=runtime_stub(cap=16)
        for _ in range(10):c._emit('QA')
        with self.assertRaises(BufferError):c.prepare(r,local(r))
        self.assertFalse(c.journal.pending)

    def test_same_time_advance_is_duplicate_not_reward(self):
        r,c=runtime_stub();begin(r,c);c.advance(r,timeline_end=False);c.advance(r,timeline_end=False)
        self.assertEqual(0,c.actual_updates);self.assertEqual(0,next(iter(c.journal.pending.values()))['step_count'])

    def test_cost_route_local_wait_interrupts_but_continue_retains_cost_interval(self):
        r,c=runtime_stub();c.roles['consumer']['setting']['interval_mode']='completed_route'
        begin_result=c.prepare(r,local(r))
        self.assertTrue(begin_result['monitor_only'])
        c.accept(r,begin_result,PlanExecutionResult('p','continue','continue','applied',1))
        self.assertEqual(0,c.actual_updates)
        # No local action is falsely promoted to completed route cost.
        self.assertFalse(c.contexts)

    def test_missing_stage_history_is_unavailable_not_inferred(self):
        from traci.exceptions import TraCIException
        from crowdsim.decision.route_provider import RouteCandidate,bind_candidates
        r,c=runtime_stub();c.roles['consumer']['setting']['interval_mode']='completed_route'
        cs=bind_candidates((RouteCandidate('goal','b',5,('a','b'),10),),'p',r.snapshot_id)
        r.decision_candidates={'p':cs}
        plan=replace(local(r),proposed_action='reroute',target_id='goal',route_edges=cs[0].edges,
            arrival_position=5,candidate_id=cs[0].candidate_id)
        prepared=c.prepare(r,plan);c.accept(r,prepared,PlanExecutionResult('p','reroute','reroute','applied',1))
        next_step(r,2)
        p,s,o=fixtures(2,stage_type=1);r.observations={'p':o};r.current.persons={'p':o.own_motion}
        r.adapter.current_person_stage=lambda pid:SimpleNamespace(**stage(kind=1,desc='waiting').as_dict())
        r.adapter.previous_person_stage=Mock(side_effect=TraCIException('QA missing history'))
        c.advance(r,timeline_end=False);self.assertEqual(0,c.accepted_samples)
        row=next(json.loads(x)['evidence'] for x in c.batch() if json.loads(x)['kind']=='interval')
        self.assertEqual('stage_change_without_completion',row['end_reason']);self.assertIsNone(row.get('observed_cost_seconds'))

    def test_other_target_or_onward_plan_not_part_of_refinement(self):
        from crowdsim.decision.route_provider import RouteCandidate,bind_candidates
        r,c=runtime_stub();cs=bind_candidates((RouteCandidate('goal','b',5,('a','b'),10),
            RouteCandidate('other','b',5,('a','b'),1),RouteCandidate('goal','b',6,('a','b'),1)), 'p',r.snapshot_id)
        r.decision_candidates={'p':cs}
        plan=replace(local(r),proposed_action='reroute',target_id='goal',route_edges=cs[0].edges,arrival_position=5,candidate_id=cs[0].candidate_id)
        self.assertEqual(plan,c.refine(r,plan));valid=c._plans(r,plan)
        self.assertEqual(1,len(valid));self.assertEqual(cs[0].candidate_id,valid[0][0].candidate_id)

    def test_no_update_or_suggestion_from_unconfigured_role(self):
        r,c=runtime_stub();r.population.profile_for=lambda pid:replace(fixtures()[0],crowd_role='commuter')
        self.assertEqual(local(r),c.refine(r,local(r)))
        self.assertIsNone(c.prepare(r,local(r)));self.assertFalse(c.learners)

    def test_update_audit_serialization_failure_rolls_back_learner(self):
        r,c=runtime_stub();begin(r,c);next_step(r,2);c.advance(r,timeline_end=False)
        c.refine(r,local(r));self.assertEqual(1,c.actual_updates)
        # Independently check serialize failure before queue insertion leaves no duplicate record.
        before=c.sequence
        with self.assertRaises(TypeError):c._emit('qa',unsupported=object())
        self.assertEqual(before,c.sequence)

    def test_real_route_interval_continue_retained_wait_supersedes_without_reward(self):
        from crowdsim.decision.route_provider import RouteCandidate,bind_candidates
        r,c=runtime_stub();c.roles['consumer']['setting']['interval_mode']='completed_route'
        cs=bind_candidates((RouteCandidate('goal','b',5,('a','b'),10),),'p',r.snapshot_id)
        r.decision_candidates={'p':cs}
        route=replace(local(r),proposed_action='reroute',target_id='goal',route_edges=cs[0].edges,
            arrival_position=5,candidate_id=cs[0].candidate_id)
        prepared=c.prepare(r,route);c.accept(r,prepared,PlanExecutionResult('p','reroute','reroute','applied',1))
        identity=c.contexts['p']['interval_id']
        c.accept(r,c.prepare(r,local(r)),PlanExecutionResult('p','continue','continue','applied',1))
        self.assertIn(identity,c.journal.pending)
        wait=replace(local(r),proposed_action='wait',wait_until=3)
        c.accept(r,c.prepare(r,wait),PlanExecutionResult('p','wait','wait','applied',1))
        self.assertFalse(c.journal.pending);self.assertEqual(0,c.actual_updates)
        row=next(json.loads(x)['evidence'] for x in c.batch() if json.loads(x)['kind']=='interval')
        self.assertEqual('plan_superseded',row['end_reason'])

    def test_invalid_original_plan_is_not_rescued_by_refinement(self):
        from crowdsim.decision.route_provider import RouteCandidate,bind_candidates
        r,c=runtime_stub();cs=bind_candidates((RouteCandidate('goal','b',5,('a','b'),10),),'p',r.snapshot_id)
        r.decision_candidates={'p':cs}
        route=replace(local(r),proposed_action='reroute',target_id='goal',route_edges=('wrong',),arrival_position=5,candidate_id=cs[0].candidate_id)
        self.assertEqual(route,c.refine(r,route));self.assertFalse(c._plans(r,route))
        self.assertFalse(c.batch())

    def test_current_pad_is_frozen_once_at_real_boundary(self):
        r,c=runtime_stub();p,s,o=fixtures();r.population.profile_for=lambda pid:replace(p,crowd_role='social_visitor')
        cfg=settings();cfg['encoding']['emotion_participates']=True
        enc=FeatureEncoder(columns=tuple(cfg['encoding']['columns']),scales=cfg['encoding']['scales'],
            state_bins={k:tuple(v) for k,v in cfg['encoding']['state_bins'].items()},emotion_participates=True)
        cfg['learning_config']['feature_schema_version']=enc.fingerprint
        c=RoleLearningController({'social_visitor':cfg},capacity=100);c.attach(r,model_fingerprint='qa')
        model=r.decision_engine.assemblies['social_visitor'].emotion
        with patch.object(model,'update',wraps=model.update) as update:
            pad=c._pad(r,'p','social_visitor');self.assertEqual(pad,c._pad(r,'p','social_visitor'))
            self.assertEqual(1,update.call_count)
            next_step(r,2);c._pad(r,'p','social_visitor');self.assertEqual(2,update.call_count)

    def test_exact_learning_update_serialization_failure_restores_append_and_parameters(self):
        r,c=runtime_stub();begin(r,c);next_step(r,2);c.advance(r,timeline_end=False)
        before=c.learners['p'].export_checkpoint();emit=c._emit
        def fail(kind,**values):
            if kind=='learning_update':raise TypeError('QA update serialization')
            return emit(kind,**values)
        with patch.object(c,'_emit',side_effect=fail):
            with self.assertRaises(TypeError):c.refine(r,local(r))
        self.assertEqual(before,c.learners['p'].export_checkpoint());self.assertEqual(0,c.actual_updates)
        c.refine(r,local(r));self.assertEqual(1,c.actual_updates)

    def test_actual_runtime_reset_drops_controller_and_old_pending_policy(self):
        from tests.test_backend_merge_contracts import RuntimeCompatibilityTests
        r=RuntimeCompatibilityTests().runtime();self.addCleanup(r.close)
        r.decision_engine.configure(compiled_model()['individual_model']['agent_types'],execution_contract='candidate-decision-v1')
        c=RoleLearningController({'consumer':settings()},capacity=100)
        c.attach(r,model_fingerprint='sha256:qa');r.learning_controller=c;old=r.run_id
        with patch.object(SimulationRuntime,'initialize',return_value='QA initialized'), patch(
                'crowdsim.infrastructure.sumo_adapter.discover_sumo_binary',return_value='unused'):
            self.assertEqual('QA initialized',r.reset())
        self.assertTrue(c.closed);self.assertIsNone(r.learning_controller);self.assertNotEqual(old,r.run_id)
        with self.assertRaises(ValueError):c.refine(r,local(r))

    def test_replay_remains_method_specific_and_explicit(self):
        cfg=settings('tabular_q_v1');cfg['learning_config']['replay']={'enabled':True,'capacity':10,'batch_size':1}
        c=RoleLearningController({'consumer':cfg},capacity=100)
        r,_=runtime_stub();c.attach(r,model_fingerprint='qa')
        self.assertTrue(c._person(r,'p').replay['enabled'])
        cfg=settings('linucb_v1');cfg['learning_config']['replay']={'enabled':False}
        with self.assertRaises(ValueError):RoleLearningController({'consumer':cfg},capacity=100)

    def test_completed_route_rejects_action_target_but_accepts_route_goal_only(self):
        # completed_route records local actions as monitor_only; an action-learning
        # target is incompatible and must be rejected rather than faked into the interval.
        cfg=settings('bounded_adaptation_v1','completed_route');cfg['learning_config']['targets']=['goal_route','action']
        with self.assertRaisesRegex(ValueError,'decision_interval'):RoleLearningController({'consumer':cfg},capacity=100)
        cfg['learning_config']['targets']=['goal_route']
        c=RoleLearningController({'consumer':cfg},capacity=100)
        self.assertEqual('completed_route',c.diagnostics()['roles']['consumer']['interval_mode'])

    def test_recorder_partial_write_rolls_back_only_new_append(self):
        from io import StringIO
        handle=StringIO('previous\n')
        handle.__enter__=lambda:handle
        class Partial:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def seek(self,*args):return handle.seek(*args)
            def tell(self):return handle.tell()
            def truncate(self):return handle.truncate()
            def write(self,text):handle.write(text[:5]);raise OSError('QA partial append')
        recorder=ExperimentRecorder.__new__(ExperimentRecorder);recorder.directory=Path('/qa')
        with patch.object(Path,'open',return_value=Partial()):
            with self.assertRaises(OSError):recorder.record_learning_runtime(('{"kind":"new"}',))
        self.assertEqual('previous\n',handle.getvalue())

    def test_recorder_writes_frozen_audit_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder=ExperimentRecorder.__new__(ExperimentRecorder);recorder.directory=Path(tmp)
            batch=('{"kind":"qa","reward":null}',)
            recorder.record_learning_runtime(batch)
            self.assertEqual([{'kind':'qa','reward':None}], [json.loads(x) for x in (Path(tmp)/'learning_runtime.jsonl').read_text().splitlines()])


    def test_bounded_completed_route_terminal_sample_update_then_later_refine(self):
        # End-to-end production wiring for the bounded completed_route interval:
        # terminal stage -> real evaluated sample -> observe/update -> the next
        # refine still reaches the updated individual learner. No second-arm
        # swap is claimed here; the long natural-choice scenario stays later.
        from traci._simulation import Stage
        from crowdsim.decision.route_provider import RouteCandidate, bind_candidates
        p, s, o = fixtures()
        e = IndividualModelingEngine()
        e.configure(compiled_model()['individual_model']['agent_types'], execution_contract='candidate-decision-v1')
        r = SimpleNamespace(decision_engine=e, use_llm=False, run_id='r', step_length=1,
            visual_policy=SimpleNamespace(low_speed_threshold_mps=.2),
            population=SimpleNamespace(profile_for=lambda pid: p, states={'p': s}, locked_itinerary_ids=set(),
                ledger=SimpleNamespace(unknown_disappearances=set(), explicitly_removed={}, arrived_ids=set())),
            observations={'p': o}, current=SimpleNamespace(persons={'p': o.own_motion}),
            snapshot_id=o.snapshot_id, time_seconds=o.time_seconds, decision_candidates={'p': ()},
            adapter=SimpleNamespace(current_person_stage=lambda pid: SimpleNamespace(**stage(kind=2, edges=('a', 'b'), desc='walk').as_dict())),
            plan_executor=SimpleNamespace(validate_plan=Mock()))
        r._post_action_constraints = lambda pid: deepcopy(HOLDS)
        c = RoleLearningController({'consumer': settings('bounded_adaptation_v1', 'completed_route')}, capacity=100)
        c.attach(r, model_fingerprint='sha256:qa')

        cs = bind_candidates((RouteCandidate('goal', 'b', 5, ('a', 'b'), 10),
                              RouteCandidate('goal2', 'd', 6, ('c', 'd'), 8)), 'p', o.snapshot_id)
        r.decision_candidates = {'p': cs}
        plan = BehaviorPlan('p', o.snapshot_id, 'reroute', decided_at=1, target_id='goal',
            route_edges=('a', 'b'), arrival_position=5, candidate_id=cs[0].candidate_id,
            scheduling_source='configured_rule')
        prepared = c.prepare(r, plan)
        self.assertIsNotNone(prepared)
        c.accept(r, prepared, PlanExecutionResult('p', 'reroute', 'reroute', 'applied', 1))

        p2, s2, o2 = fixtures(2)
        r.time_seconds = 2; r.snapshot_id = o2.snapshot_id; r.current.persons = {'p': o2.own_motion}
        r.observations = {'p': o2}; r.population.states = {'p': s2}
        # The successor candidates are bound to the current boundary snapshot,
        # mirroring the production per-frame candidate rebinding.
        cs2 = bind_candidates((RouteCandidate('goal', 'b', 5, ('a', 'b'), 10),
                               RouteCandidate('goal2', 'd', 6, ('c', 'd'), 8)), 'p', o2.snapshot_id)
        r.decision_candidates = {'p': cs2}
        r.adapter.current_person_stage = lambda pid: SimpleNamespace(**stage(kind=2, edges=('c', 'd'), desc='walk2').as_dict())
        done_stage = Stage(type=2, edges=('a', 'b'), arrivalPos=5, depart=1, departPos=0, travelTime=1, description='walk')
        r.adapter.previous_person_stage = Mock(return_value=done_stage)
        c.advance(r, timeline_end=False)

        self.assertEqual(1, c.accepted_samples)
        self.assertEqual(1, c.actual_updates)
        rows = [json.loads(x) for x in c.batch()]
        update = next(x for x in rows if x['kind'] == 'learning_update')
        sample = update['sample']
        self.assertEqual('goal', sample['target_id'])
        self.assertEqual(cs[0].candidate_id, sample['candidate_id'])
        self.assertTrue(sample['features']); self.assertTrue(sample['state'])
        self.assertTrue(sample['cost_complete'])
        self.assertTrue(update['updates'])
        # Each per-sample update record carries the immutable payload projection,
        # not only a sample id / digest.
        update_sample = update['updates'][0]['sample']
        self.assertEqual(sample['sample_id'], update_sample['sample_id'])
        self.assertEqual(sample['action'], update_sample['action'])
        self.assertEqual(sample['target_id'], update_sample['target_id'])
        self.assertEqual(sample['candidate_id'], update_sample['candidate_id'])
        self.assertEqual(sample['state'], update_sample['state'])
        self.assertEqual(sample['features'], update_sample['features'])
        self.assertEqual(sample['reward'], update_sample['reward'])

        learner = c.learners['p']
        correction_key = tuple(sample['action'])
        self.assertIn(correction_key, learner.corrections)
        # Observed completed cost (1s) is below the predicted cost (10s), so the
        # bounded core stores a negative cost correction on this route arm.
        self.assertLess(learner.corrections[correction_key], 0)

        # The later boundary scores through the warmed, updated individual core;
        # the correction enters the advisory score. No second-arm natural swap is
        # claimed here; the long natural-choice scenario stays later.
        later_plan = replace(plan, snapshot_id=o2.snapshot_id, decided_at=2,
            candidate_id=cs2[0].candidate_id)
        scores = c.candidate_scores(r, later_plan)
        self.assertTrue(scores)
        entry = scores[cs2[0].candidate_id]
        self.assertIsNotNone(entry['score'])
        expected = -(10 + learner.corrections[correction_key])
        self.assertAlmostEqual(entry['score'], expected, places=6)

    def test_bounded_with_llm_refine_keeps_bounded_core_without_silent_fallback(self):
        # v6 assembly: bounded_adaptation_v1 combined with llm_baseline_refine.
        # The LLM remains decision owner; the attached learner must stay the bounded
        # core (created by method id), never silently replaced by linucb or skipped.
        from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
        from crowdsim.modeling_algorithm.individual_model.learning.learner_cores import BoundedAdaptationCore
        p, s, o = fixtures()
        e = IndividualModelingEngine()
        e.configure(compiled_model()['individual_model']['agent_types'], execution_contract='candidate-decision-v1')
        e.assemblies['consumer'].decision = LLMDrivenDecisionModel()
        r = SimpleNamespace(decision_engine=e, use_llm=True, run_id='r', step_length=1,
            visual_policy=SimpleNamespace(low_speed_threshold_mps=.2),
            population=SimpleNamespace(profile_for=lambda pid: p, states={'p': s}, locked_itinerary_ids=set(),
                ledger=SimpleNamespace(unknown_disappearances=set(), explicitly_removed={}, arrived_ids=set())),
            observations={'p': o}, current=SimpleNamespace(persons={'p': o.own_motion}),
            snapshot_id=o.snapshot_id, time_seconds=o.time_seconds, decision_candidates={'p': ()},
            adapter=SimpleNamespace(current_person_stage=lambda pid: SimpleNamespace(**stage(kind=2, edges=('a', 'b'), desc='walk').as_dict())),
            plan_executor=SimpleNamespace(validate_plan=Mock()))
        r._post_action_constraints = lambda pid: deepcopy(HOLDS)
        c = RoleLearningController({'consumer': settings('bounded_adaptation_v1', 'completed_route',
            integration='llm_baseline_refine')}, capacity=100)
        c.attach(r, model_fingerprint='sha256:qa')
        learner = c._person(r, 'p')
        self.assertIsInstance(learner, BoundedAdaptationCore)
        self.assertEqual('bounded_adaptation_v1', learner.method_id)


if __name__=='__main__':unittest.main()
