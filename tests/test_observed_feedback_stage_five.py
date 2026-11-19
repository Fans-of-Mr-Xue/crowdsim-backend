"""Observed features/intervals: explicit QA encoding, actual SUMO stage costs."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import math
import unittest
from traci._simulation import Stage
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, Observation, BehaviorPlan, PlanExecutionResult
from crowdsim.decision.route_provider import RouteCandidate, bind_candidates
from crowdsim.modeling_algorithm.individual_model.learning.observed_feedback import (
    FeatureEncoder, StageEvidence, ObservationPoint, PhysicalIntervalJournal, encode_plan_action)
from tests.test_poi_replanning import motion

ROOT=Path(__file__).resolve().parents[1]
HOLDS={'flow_hold':[],'activity_hold':False,'policy_wait':False,'control_limits':{}}


def fixtures(now=1,pid='p',snapshot=None,stage_type=2):
    p=AgentProfile(pid,crowd_role='consumer',free_walking_speed=2)
    s=AgentState(pid,stress=.2,fatigue=.3)
    m=replace(motion(),person_id=pid,time_seconds=now,speed=1.,stage_type=stage_type,remaining_stage_count=2)
    o=Observation(pid,snapshot or f'r:{now}',now,m,objective_density_per_m2=2.,perceived_risk=.2,perceived_crowding=.4)
    return p,s,o


def stage(kind=2,edges=('a','b'),desc='walking',depart=1,travel=0,arrival=5):
    return StageEvidence.capture(Stage(type=kind,edges=list(edges),arrivalPos=arrival,depart=depart,
        departPos=0,travelTime=travel,description=desc))


def point(now=1,*,risk=.2,density=2.,holds=None,st=None,pid='p'):
    p,s,o=fixtures(now,pid=pid,stage_type=st.as_dict()['type'] if st else 2)
    o=replace(o,perceived_risk=risk,objective_density_per_m2=density)
    return ObservationPoint.capture(p,s,o,constraints=deepcopy(HOLDS if holds is None else holds),stage=st or stage())


def journal(cap=20,step=1):
    return PhysicalIntervalJournal('r',model_fingerprint='sha256:qa',step_seconds=step,low_speed_threshold=.2,capacity=cap)


def start(j,*,mode='decision_interval',st=None):
    plan=BehaviorPlan('p','r:1','continue',decided_at=1)
    cs=()
    if mode=='completed_route':
        cs=bind_candidates((RouteCandidate('goal','b',5,('a','b'),10),),'p','r:1')
        plan=replace(plan,proposed_action='reroute',target_id='goal',candidate_id=cs[0].candidate_id,
                     route_edges=cs[0].edges,arrival_position=5)
    receipt=PlanExecutionResult('p',plan.proposed_action,plan.proposed_action,'applied',1)
    return j.begin('i',point(st=st),plan,receipt,cs,mode=mode)


class ObservedFeedbackTests(unittest.TestCase):
    def test_feature_columns_scales_bins_and_raw_evidence_detach(self):
        encoder=FeatureEncoder(columns=('bias','speed_ratio','objective_density'),
            scales={'bias':1,'speed_ratio':1,'objective_density':4},
            state_bins={'perceived_risk':(.1,.5),'objective_density':(1.,3.)},emotion_participates=False)
        p,s,o=fixtures();plan=BehaviorPlan('p',o.snapshot_id,'continue',decided_at=1)
        encoded=encoder.encode(p,s,o,plan,())
        self.assertEqual((1.,.5,.5),encoded.features)
        self.assertEqual((1,1),encoded.state)
        row=encoded.as_dict();row['raw_values']['objective_density']=999
        self.assertEqual(2,encoded.as_dict()['raw_values']['objective_density'])
        schema=encoder.schema;schema['columns'].clear();self.assertEqual(3,len(encoder.schema['columns']))

    def test_explicit_encoding_rejects_missing_density_bad_scales_or_pad_participation(self):
        encoder=FeatureEncoder(columns=('objective_density',),scales={'objective_density':1},
            state_bins={'perceived_risk':()},emotion_participates=False)
        p,s,o=fixtures();o=replace(o,objective_density_per_m2=None)
        with self.assertRaisesRegex(ValueError,'unavailable'):encoder.encode(p,s,o,BehaviorPlan('p',o.snapshot_id,'continue',decided_at=1),())
        for scales in ({},{'objective_density':0},{'objective_density':True}):
            with self.assertRaises(ValueError):FeatureEncoder(columns=('objective_density',),scales=scales,state_bins={'perceived_risk':()},emotion_participates=False)
        with self.assertRaises(ValueError):FeatureEncoder(columns=('pad_pleasure',),scales={'pad_pleasure':1},state_bins={'perceived_risk':()},emotion_participates=False)

    def test_allowed_pad_is_explicit_and_does_not_update_emotion(self):
        e=FeatureEncoder(columns=('pad_pleasure','pad_arousal'),scales={'pad_pleasure':1,'pad_arousal':1},
            state_bins={'pad_dominance':(0.,)},emotion_participates=True)
        p,s,o=fixtures();plan=BehaviorPlan('p',o.snapshot_id,'continue',decided_at=1)
        with self.assertRaises(ValueError):e.encode(p,s,o,plan,())
        pad={'pleasure':.2,'arousal':-.4,'dominance':.1};copy=deepcopy(pad)
        self.assertEqual((.2,-.4),e.encode(p,s,o,plan,(),pad=pad).features);self.assertEqual(copy,pad)

    def test_wait_semantics_independent_of_clock_speed_limits_distinct(self):
        p,s,o=fixtures();p2,s2,o2=fixtures(now=20)
        first=BehaviorPlan('p',o.snapshot_id,'wait',wait_until=4,decided_at=1)
        later=replace(first,snapshot_id=o2.snapshot_id,wait_until=23,decided_at=20)
        self.assertEqual(encode_plan_action(first,o,()),encode_plan_action(later,o2,()))
        slow=replace(first,proposed_action='slow_down',wait_until=None,speed_limit=.4)
        self.assertNotEqual(encode_plan_action(slow,o,()),encode_plan_action(replace(slow,speed_limit=.5),o,()))
        with self.assertRaises(ValueError):encode_plan_action(replace(first,wait_until=1),o,())

    def test_candidate_features_use_bound_payload_and_semantics_reuse(self):
        e=FeatureEncoder(columns=('candidate_cost','candidate_entry_density'),scales={'candidate_cost':10,'candidate_entry_density':2},state_bins={'perceived_risk':()},emotion_participates=False)
        p,s,o=fixtures();cs=bind_candidates((RouteCandidate('goal','b',5,('a','b'),10,entry_density_person_per_m2=1),),'p',o.snapshot_id)
        plan=BehaviorPlan('p',o.snapshot_id,'reroute',target_id='goal',route_edges=cs[0].edges,candidate_id=cs[0].candidate_id,arrival_position=5,decided_at=1)
        x=e.encode(p,s,o,plan,cs);self.assertEqual((1.,.5),x.features)
        with self.assertRaises(ValueError):e.encode(p,s,o,plan,(replace(cs[0],estimated_cost_seconds=20),))
        with self.assertRaises(ValueError):e.encode(p,s,o,replace(plan,route_edges=('a','c')),cs)

    def test_interval_integrals_accumulate_all_physical_steps_and_boundary(self):
        j=journal();start(j)
        self.assertEqual('pending',j.advance('i',point(2,risk=.4)))
        self.assertEqual('decision_boundary',j.advance('i',point(3,risk=.6),decision_boundary=True))
        row=j.batch()[0].as_dict()
        self.assertEqual(2,row['duration_seconds']);self.assertAlmostEqual(.8,row['risk_exposure_seconds'])
        self.assertEqual(4,row['density_exposure_person_seconds_per_m2'])
        self.assertEqual({'moving':2.},row['activity_seconds']);self.assertFalse(row['cost_complete'])
        self.assertIsNone(row['reward']);self.assertFalse(row['trainable'])

    def test_missing_density_stays_unavailable_not_zero_reward(self):
        j=journal();start(j);j.advance('i',point(2,density=None),decision_boundary=True)
        row=j.batch()[0].as_dict();self.assertIsNone(row['density_exposure_person_seconds_per_m2']);self.assertIsNone(row['reward'])

    def test_hold_classifications_use_left_boundary_not_blocked_duration(self):
        for key,label in [('flow_hold','flow_hold'),('activity_hold','activity_hold'),('policy_wait','policy_wait')]:
            j=journal();holds=deepcopy(HOLDS);holds[key]=['hold'] if key=='flow_hold' else True
            plan=BehaviorPlan('p','r:1','continue',decided_at=1)
            j.begin('i',point(holds=holds),plan,PlanExecutionResult('p','continue','continue','applied',1),(),mode='decision_interval')
            j.advance('i',point(2),decision_boundary=True)
            self.assertEqual({label:1.},j.batch()[0].as_dict()['activity_seconds'])

    def test_rejected_and_external_controls_cannot_train(self):
        j=journal();plan=BehaviorPlan('p','r:1','continue',decided_at=1)
        with self.assertRaises(ValueError):j.begin('i',point(),plan,PlanExecutionResult('p','continue','continue','rejected',1),(),mode='decision_interval')
        start(j);holds=deepcopy(HOLDS);holds['control_limits']={'controller':.3}
        self.assertEqual('external_control',j.advance('i',point(2,holds=holds),decision_boundary=True))
        self.assertTrue(j.batch()[0].as_dict()['truncated']);self.assertFalse(j.batch()[0].as_dict()['trainable'])

    def test_duplicate_gap_wrong_run_and_role_do_not_half_update(self):
        j=journal();start(j);old=deepcopy(j.pending)
        self.assertEqual('duplicate',j.advance('i',point()))
        for bad in (point(3),point(2,pid='q'),ObservationPoint(point(2).payload_json.replace('r:2','other:2'))):
            with self.assertRaises(ValueError):j.advance('i',bad)
            self.assertEqual(old,j.pending)
        with self.assertRaises(ValueError):j.advance('i',point(2),decision_boundary='true')

    def test_route_requires_completed_stage_and_exact_matching_identity(self):
        j=journal();start(j,mode='completed_route')
        new=stage(kind=1,edges=('b',),desc='wait',depart=1,travel=2)
        j.advance('i',point(2,st=new),completed_stage=stage(depart=1,travel=1))
        row=j.batch()[0].as_dict();self.assertTrue(row['cost_complete']);self.assertEqual(1,row['observed_cost_seconds'])
        self.assertFalse(row['terminated']);self.assertEqual('route_stage_completed',row['end_reason'])
        for completed in (None,stage(desc='other',depart=1,travel=1),stage(depart=0,travel=2)):
            j=journal();start(j,mode='completed_route');j.advance('i',point(2,st=new),completed_stage=completed)
            row=j.batch()[0].as_dict();self.assertFalse(row['cost_complete']);self.assertTrue(row['truncated'])

    def test_timeline_reset_arrival_do_not_fabricate_complete_cost(self):
        j=journal();start(j,mode='completed_route');j.advance('i',point(2),timeline_end=True)
        self.assertTrue(j.batch()[0].as_dict()['truncated']);self.assertFalse(j.batch()[0].as_dict()['cost_complete'])
        for reason in ('runtime_reset','sumo_arrived_without_stage','unknown_disappearance'):
            j=journal();start(j,mode='completed_route');j.interrupt('i',reason=reason,now=1)
            row=j.batch()[0].as_dict();self.assertFalse(row['cost_complete']);self.assertIsNone(row.get('observed_cost_seconds'))
            self.assertFalse(row['terminated'] and row['truncated'])

    def test_route_boundary_does_not_end_route_and_capacity_ack_is_atomic(self):
        j=journal(cap=1);start(j,mode='completed_route')
        self.assertEqual('pending',j.advance('i',point(2),decision_boundary=True))
        j.interrupt('i',reason='runtime_closed',now=2);batch=j.batch()
        with self.assertRaises(ValueError):j.acknowledge((batch[0],batch[0]))
        self.assertEqual(batch,j.batch());j.acknowledge(batch)
        self.assertEqual('duplicate',start(j))
        # Ack drained the in-flight slot; historical dedup memory must not exhaust
        # capacity, so a fresh interval can begin even after a long history.
        plan=BehaviorPlan('p','r:1','continue',decided_at=1)
        receipt=PlanExecutionResult('p','continue','continue','applied',1)
        self.assertEqual('started',j.begin('new',point(),plan,receipt,(),mode='decision_interval'))
        self.assertEqual(1,j.active_count)

    def test_capacity_guard_counts_in_flight_backlog_not_historical_seen(self):
        j=journal(cap=1);start(j)
        plan=BehaviorPlan('p','r:1','continue',decided_at=1)
        receipt=PlanExecutionResult('p','continue','continue','applied',1)
        # One open interval already fills the in-flight capacity: a second is rejected.
        with self.assertRaises(BufferError):j.begin('new',point(),plan,receipt,(),mode='decision_interval')
        # Drain it; the recycled slot admits a fresh interval even though 'i' was seen.
        j.interrupt('i',reason='runtime_closed',now=2);j.acknowledge(j.batch())
        self.assertEqual('started',j.begin('new',point(),plan,receipt,(),mode='decision_interval'))

    def test_dedup_window_recycles_old_ids_without_growing_unbounded(self):
        j=journal(cap=1)
        plan=BehaviorPlan('p','r:1','continue',decided_at=1)
        receipt=PlanExecutionResult('p','continue','continue','applied',1)
        self.assertEqual('started',j.begin('a',point(),plan,receipt,(),mode='decision_interval'))
        j.interrupt('a',reason='runtime_closed',now=2);j.acknowledge(j.batch())
        self.assertEqual('started',j.begin('b',point(),plan,receipt,(),mode='decision_interval'))
        j.interrupt('b',reason='runtime_closed',now=2);j.acknowledge(j.batch())
        # 'a' has been evicted from the bounded dedup window; it does not block a begin.
        self.assertEqual('started',j.begin('a',point(),plan,receipt,(),mode='decision_interval'))
        self.assertEqual(1,len(j.seen))

    def test_small_capacity_ack_cycles_exceed_capacity_without_drop(self):
        # A tiny in-flight cap still sustains many drained intervals. Acking a
        # batch frees the active slot; completed evidence stays drained, and the
        # historical seen set must not refill capacity after N > capacity cycles.
        j=journal(cap=1)
        plan=BehaviorPlan('p','r:1','continue',decided_at=1)
        receipt=PlanExecutionResult('p','continue','continue','applied',1)
        cycles=5
        for n in range(cycles):
            pid=f'p{n}'
            item=replace(plan,person_id=pid);rec=replace(receipt,person_id=pid)
            self.assertEqual('started',j.begin(f'c{n}',point(pid=pid),item,rec,(),mode='decision_interval'))
            j.interrupt(f'c{n}',reason='runtime_closed',now=n+2)
            batch=j.batch()
            self.assertEqual(1,len(batch))
            j.acknowledge(batch)
            self.assertEqual(0,j.active_count)
        self.assertEqual(cycles,j.counts['started'])
        self.assertEqual(cycles,j.counts['completed'])
        self.assertEqual(0,j.counts.get('duplicates',0))
        self.assertEqual(0,j.active_count)

    def test_late_duplicate_within_tombstone_window_is_rejected_not_reapplied(self):
        # After ack the interval id remains in the bounded tombstone window. A
        # replayed receipt with the same id must be counted as a duplicate, must
        # not open a second active interval, and must not add another sample.
        j=journal(cap=2)
        plan=BehaviorPlan('p','r:1','continue',decided_at=1)
        receipt=PlanExecutionResult('p','continue','continue','applied',1)
        self.assertEqual('started',j.begin('a',point(),plan,receipt,(),mode='decision_interval'))
        self.assertEqual('started',j.begin('b',point(pid='p2'),replace(plan,person_id='p2'),
                                           replace(receipt,person_id='p2'),(),mode='decision_interval'))
        j.interrupt('a',reason='runtime_closed',now=2);j.acknowledge(j.batch())
        j.interrupt('b',reason='runtime_closed',now=2);j.acknowledge(j.batch())
        started_before=j.counts['started']
        self.assertEqual('duplicate',j.begin('a',point(),plan,receipt,(),mode='decision_interval'))
        self.assertEqual(started_before,j.counts['started'])
        self.assertEqual(1,j.counts['duplicates'])
        self.assertEqual(0,j.active_count)
        self.assertFalse(j.pending)

    def test_constraints_missing_and_unknown_stage_reject(self):
        p,s,o=fixtures()
        with self.assertRaises(ValueError):ObservationPoint.capture(p,s,o,constraints={},stage=stage())
        with self.assertRaises(ValueError):StageEvidence.capture(Stage(type=-1,edges=['a']))
        with self.assertRaises(ValueError):FeatureEncoder(columns=('bias',),scales={'bias':1},state_bins={'person_id':()},emotion_participates=False)

    def test_decision_close_at_already_observed_boundary_does_not_double_integrate(self):
        j=journal();start(j);new=point(2,risk=.4);j.advance('i',new)
        self.assertEqual('decision_boundary',j.close_decision_interval('i',new))
        row=j.batch()[0].as_dict();self.assertEqual(1,row['step_count'])
        self.assertAlmostEqual(.3,row['risk_exposure_seconds'])
        j=journal();start(j,mode='completed_route');j.advance('i',new)
        with self.assertRaises(ValueError):j.close_decision_interval('i',new)

    def test_invalid_plan_route_locks_and_expired_receipts_reject_before_begin(self):
        j=journal();plan=BehaviorPlan('p','r:1','continue',decided_at=1)
        for bad in (replace(plan,expires_at=.5),replace(plan,arrival_position=5),
                    replace(plan,proposed_action='wait',wait_until=1),replace(plan,decided_at=0)):
            rec=PlanExecutionResult('p',bad.proposed_action,bad.proposed_action,'applied',1)
            with self.assertRaises(ValueError):j.begin('bad',point(),bad,rec,(),mode='decision_interval')
        self.assertFalse(j.pending);self.assertFalse(j.seen)

    def test_boundary_completion_timing_and_evidence_are_detached(self):
        j=journal();start(j,mode='completed_route')
        wait=stage(kind=1,edges=('b',),desc='wait')
        j.advance('i',point(2,st=wait),completed_stage=stage(depart=1,travel=5))
        row=j.batch()[0].as_dict();self.assertFalse(row['cost_complete'])
        self.assertEqual('stage_completion_time_mismatch',row['end_reason'])
        row['before']['motion']['speed']=999
        self.assertEqual(1,j.batch()[0].as_dict()['before']['motion']['speed'])

    def test_state_bins_and_feature_fingerprint_bind_thresholds_and_dimensions(self):
        settings=dict(columns=('bias',),scales={'bias':1},state_bins={'perceived_risk':(.2,)},emotion_participates=False)
        e=FeatureEncoder(**settings);p,s,o=fixtures();plan=BehaviorPlan('p','r:1','continue',decided_at=1)
        self.assertEqual((1,),e.encode(p,s,o,plan,()).state)
        other=FeatureEncoder(**{**settings,'state_bins':{'perceived_risk':(.3,)}})
        self.assertNotEqual(e.fingerprint,other.fingerprint)
        for bins in ((.2,.1),(.2,.2),(float('nan'),)):
            with self.assertRaises(ValueError):FeatureEncoder(**{**settings,'state_bins':{'perceived_risk':bins}})

    def test_interruption_tail_is_unobserved_not_integrated_and_stale_time_rejects(self):
        j=journal();start(j);j.advance('i',point(2,risk=.4))
        before=deepcopy(j.pending)
        with self.assertRaises(ValueError):j.interrupt('i',reason='missing_observation',now=1)
        self.assertEqual(before,j.pending)
        j.interrupt('i',reason='missing_observation',now=3)
        row=j.batch()[0].as_dict()
        self.assertEqual(2,row['ended_at']);self.assertEqual(3,row['interrupted_at'])
        self.assertEqual(1,row['unobserved_tail_seconds']);self.assertAlmostEqual(.3,row['risk_exposure_seconds'])
        self.assertIsNone(row['unplanned_blocking_seconds']);self.assertIsNone(row['goal_progress'])

    def test_same_boundary_conflict_and_stage_mismatch_are_rejected(self):
        j=journal();start(j);before=deepcopy(j.pending)
        with self.assertRaises(ValueError):j.advance('i',point(1,risk=.8))
        self.assertEqual(before,j.pending)
        p,s,o=fixtures()
        with self.assertRaises(ValueError):ObservationPoint.capture(p,s,o,constraints=HOLDS,
            stage=stage(kind=1,edges=('a',)))
        with self.assertRaises(ValueError):FeatureEncoder(columns=('bias',),scales={'bias':1},
            state_bins={'perceived_risk':()},emotion_participates=False,version='unknown')

    def test_previous_stage_read_propagates_missing_history_without_fallback(self):
        from crowdsim.infrastructure.sumo_adapter import SumoAdapter
        from unittest.mock import Mock
        adapter=SumoAdapter(ROOT/'tests/scenarios/alternative_routes/scenario.sumocfg',
            sumo_binary=str(ROOT/'.venv/sumo-1.24.0/bin/sumo'))
        adapter.connection=Mock();adapter.closed=False
        adapter.connection.person.getStage.side_effect=ValueError('no completed stage')
        with self.assertRaisesRegex(ValueError,'no completed stage'):adapter.previous_person_stage('p')
        adapter.connection.person.getStage.assert_called_once_with('p',-1)
        adapter.closed=True
        with self.assertRaises(RuntimeError):adapter.previous_person_stage('p')
        self.assertEqual(1,adapter.connection.person.getStage.call_count)

    def test_bad_controls_and_expired_feature_plans_reject(self):
        p,s,o=fixtures();plan=BehaviorPlan('p','r:1','continue',decided_at=1,expires_at=.5)
        with self.assertRaises(ValueError):encode_plan_action(plan,o,())
        for value in (True,-1,float('nan')):
            controls=deepcopy(HOLDS);controls['control_limits']={'controller':value}
            with self.assertRaises(ValueError):ObservationPoint.capture(p,s,o,constraints=controls,stage=stage())

    def test_real_sumo_route_completed_cost_comes_from_completed_stage(self):
        from crowdsim.infrastructure.sumo_adapter import SumoAdapter
        from crowdsim.infrastructure.network_adapter import ResearchNetwork
        from crowdsim.decision.route_provider import RouteProvider
        from crowdsim.decision.plan_executor import PlanExecutor
        from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
        adapter=SumoAdapter(ROOT/'tests/scenarios/alternative_routes/scenario.sumocfg',sumo_binary=str(ROOT/'.venv/sumo-1.24.0/bin/sumo'))
        self.addCleanup(adapter.close);adapter.start();result=adapter.step();pid=next(iter(result.persons));m=result.persons[pid]
        provider=RouteProvider(ResearchNetwork(ROOT/'tests/scenarios/alternative_routes/network.net.xml'),adapter)
        # A real waiting successor preserves the person's history at the completion boundary.
        arrival=float(adapter.current_person_stage(pid).arrivalPos)
        raw=provider.build_via_candidate(m,target_id='goal',target_edge='exit',via_edge='lower_a',arrival_position=arrival)
        cs=bind_candidates((raw,),pid,'r:1');p=AgentProfile(pid,crowd_role='consumer');s=AgentState(pid)
        o=Observation(pid,'r:1',result.time_seconds,m,perceived_risk=0,perceived_crowding=0,objective_density_per_m2=None)
        plan=LLMDrivenDecisionModel._make_plan(p,o,'reroute',cs[0],reason='QA',confidence=None,source='qa')
        receipt=PlanExecutor(adapter,provider).apply(plan,m,'r:1',result.time_seconds,candidates=cs,require_candidate=True)
        self.assertEqual('applied',receipt.status);adapter.append_waiting_stage(pid,5,'qa-history')
        st=StageEvidence.capture(adapter.current_person_stage(pid));after=adapter.snapshot().persons[pid]
        o=replace(o,own_motion=after)
        initial=ObservationPoint.capture(p,s,o,constraints=deepcopy(HOLDS),stage=st)
        j=journal(step=.5);j.begin('i',initial,plan,receipt,cs,mode='completed_route')
        history=[]
        for index in range(1,300):
            result=adapter.step();self.assertIn(pid,result.persons)
            m=result.persons[pid];obs=replace(o,snapshot_id=f'r:{index+1}',time_seconds=result.time_seconds,own_motion=m)
            current=StageEvidence.capture(adapter.current_person_stage(pid));done=None
            if current.identity()!=st.identity():done=StageEvidence.capture(adapter.previous_person_stage(pid))
            status=j.advance('i',ObservationPoint.capture(p,s,obs,constraints=deepcopy(HOLDS),stage=current),completed_stage=done)
            history.append(result.time_seconds)
            if status!='pending':break
        row=j.batch()[0].as_dict()
        self.assertTrue(row['cost_complete'],row['end_reason'])
        self.assertEqual(done.as_dict()['travelTime'],row['observed_cost_seconds'])
        self.assertEqual(done.identity(),st.identity())
        self.assertGreater(row['observed_cost_seconds'],.5)
        self.assertIsNone(row['reward']);self.assertFalse(row['trainable'])
        self.assertEqual(row['step_count'],len(history))

if __name__=='__main__':unittest.main()
