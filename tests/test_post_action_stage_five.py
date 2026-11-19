"""Post-action evidence is detached, attributed and never a learner/reward."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import tempfile
import unittest
from crowdsim.modeling_algorithm.individual_model.learning.post_action import PostActionJournal
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, BehaviorPlan, PlanExecutionResult, Observation
from crowdsim.core.population_manager import PopulationLedger
from crowdsim.core.simulation_runtime import SimulationRuntime, RuntimeState
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
from tests.test_poi_replanning import motion
from tests.test_candidate_decision_stage_four import engine


def context(run='r', pid='p', now=1):
    p=AgentProfile(pid,crowd_role='consumer');s=AgentState(pid)
    m=replace(motion(),person_id=pid,time_seconds=now)
    o=Observation(pid,f'{run}:1',now,m)
    return p,s,o,BehaviorPlan(pid,o.snapshot_id,'continue',decided_at=now)


def receipt(plan,status='applied',action=None):
    return PlanExecutionResult(plan.person_id,plan.proposed_action,action or plan.proposed_action,status,plan.decided_at)


class PostActionTests(unittest.TestCase):
    def prepare(self,j,p,s,o,plan,**kw):
        return j.prepare(plan,p,s,o,(),snapshot_id=o.snapshot_id,now=o.time_seconds,**kw)

    def advance(self,j,o,s,**kw):
        j.advance(snapshot_id='r:2',now=2,observations={'p':replace(o,snapshot_id='r:2',time_seconds=2,own_motion=replace(o.own_motion,time_seconds=2,speed=.7))},states={'p':s},ledger=PopulationLedger(),**kw)

    def test_after_state_comes_from_new_observation_and_before_is_immutable(self):
        j=PostActionJournal('r');p,s,o,plan=context();prepared=self.prepare(j,p,s,o,plan)
        s.stress=.8;j.accept_receipt(prepared,receipt(plan));self.advance(j,o,s)
        item=j.batch()[0];row=item.as_dict()
        self.assertEqual(0,row['before']['state']['stress'])
        self.assertEqual(.8,row['after']['state']['stress'])
        self.assertEqual(.7,row['after']['observation']['own_motion']['speed'])
        row['before']['state']['stress']=10
        self.assertEqual(0,item.as_dict()['before']['state']['stress'])
        with self.assertRaises(FrozenInstanceError):item.payload_json='x'
        self.assertIsNone(row['reward']);self.assertFalse(row['trainable'])
        self.assertEqual(0,j.diagnostics()['policy_updates'])

    def test_rejected_deferred_partial_have_no_fabricated_actual_post_state(self):
        for status in ('rejected','deferred','partial_failure'):
            j=PostActionJournal('r');p,s,o,plan=context()
            j.accept_receipt(self.prepare(j,p,s,o,plan),receipt(plan,status,'continue'))
            row=j.batch()[0].as_dict()
            self.assertEqual(status,row['execution']['status']);self.assertEqual('continue',row['execution']['applied_action'])
            self.assertIsNone(row['after']);self.assertFalse(row['trainable']);self.assertEqual(0,len(j.pending))

    def test_arrival_removal_unknown_missing_and_timeline_are_different(self):
        for kind in ('arrived','removed','unknown','missing','timeline'):
            j=PostActionJournal('r');p,s,o,plan=context();j.accept_receipt(self.prepare(j,p,s,o,plan),receipt(plan))
            ledger=PopulationLedger();obs={};states={}
            if kind=='arrived':ledger.arrived_ids.add('p')
            elif kind=='removed':ledger.explicitly_removed['p']='control'
            elif kind=='unknown':ledger.unknown_disappearances.add('p')
            elif kind=='timeline':obs={'p':replace(o,snapshot_id='r:2',time_seconds=2,own_motion=replace(o.own_motion,time_seconds=2))};states={'p':s}
            j.advance(snapshot_id='r:2',now=2,observations=obs,states=states,ledger=ledger,timeline_end=kind=='timeline')
            row=j.batch()[0].as_dict()
            self.assertEqual(kind=='arrived',row['terminated']);self.assertEqual(kind!='arrived',row['truncated'])
            self.assertEqual(kind=='timeline',row['after'] is not None)
            self.assertFalse(row['trainable']);self.assertIsNone(row['reward'])

    def test_flow_activity_policy_wait_not_unplanned_blocking(self):
        for key,label in [('flow_hold','flow_hold'),('activity_hold','activity_hold'),('policy_wait','policy_wait')]:
            j=PostActionJournal('r');p,s,o,plan=context();j.accept_receipt(self.prepare(j,p,s,o,plan),receipt(plan))
            self.advance(j,o,s,constraints={'p':{key:True}})
            self.assertEqual(label,j.batch()[0].as_dict()['observed_activity'])

    def test_mismatch_rejects_without_overwriting_pending(self):
        j=PostActionJournal('r');p,s,o,plan=context();prepared=self.prepare(j,p,s,o,plan)
        with self.assertRaises(ValueError):j.accept_receipt(prepared,replace(receipt(plan),person_id='other'))
        with self.assertRaises(ValueError):j.accept_receipt(prepared,replace(receipt(plan),applied_at=4))
        j.accept_receipt(prepared,receipt(plan))
        with self.assertRaises(ValueError):j.advance(snapshot_id='other:2',now=2,observations={},states={},ledger=PopulationLedger())
        with self.assertRaises(ValueError):j.advance(snapshot_id='r:2',now=2,observations={'p':o},states={'p':s},ledger=PopulationLedger())
        self.assertEqual(1,len(j.pending))
        # A stale rejected plan is still evidence, not an applied decision.
        j2=PostActionJournal('r');bad=replace(plan,snapshot_id='r:0')
        j2.accept_receipt(self.prepare(j2,p,s,o,bad),receipt(bad,'rejected','continue'))
        self.assertEqual('r:0',j2.batch()[0].as_dict()['plan']['snapshot_id'])

    def test_waiting_stage_and_stale_motion_are_not_fabricated_feedback(self):
        j=PostActionJournal('r');p,s,o,plan=context()
        j.accept_receipt(self.prepare(j,p,s,o,plan),receipt(plan))
        new=replace(o,snapshot_id='r:2',time_seconds=2,own_motion=replace(o.own_motion,time_seconds=1))
        with self.assertRaises(ValueError):j.advance(snapshot_id='r:2',now=2,observations={'p':new},states={'p':s},ledger=PopulationLedger())
        new=replace(new,own_motion=replace(new.own_motion,time_seconds=2,stage_type=1,speed=0))
        j.advance(snapshot_id='r:2',now=2,observations={'p':new},states={'p':s},ledger=PopulationLedger())
        self.assertEqual('waiting_stage',j.batch()[0].as_dict()['observed_activity'])

    def test_control_interrupt_is_per_person_and_control_limits_remain_evidence(self):
        j=PostActionJournal('r');p,s,o,plan=context()
        j.accept_receipt(self.prepare(j,p,s,o,plan),receipt(plan))
        q,t,v,other=context(pid='q');j.accept_receipt(self.prepare(j,q,t,v,other),receipt(other))
        j.interrupt(1,'external_control',person_ids=['q'])
        self.assertEqual(1,len(j.pending))
        self.assertEqual('q',j.batch()[0].as_dict()['person_id'])
        self.advance(j,o,s,constraints={'p':{'control_limits':{'qa':.5}}})
        self.assertEqual('external_control_observed',j.batch()[-1].as_dict()['evidence_status'])

    def test_duplicate_superseded_and_interrupt_not_double_completed(self):
        j=PostActionJournal('r');p,s,o,plan=context();prepared=self.prepare(j,p,s,o,plan)
        j.accept_receipt(prepared,receipt(plan));j.accept_receipt(prepared,receipt(plan))
        self.assertEqual(1,len(j.pending));self.assertEqual(1,j.counts['duplicates'])
        other=replace(plan,proposed_action='slow_down',speed_limit=.4)
        j.accept_receipt(self.prepare(j,p,s,o,other),receipt(other))
        self.assertEqual('plan_superseded',j.batch()[0].as_dict()['end_reason'])
        j.interrupt(1,'runtime_closed');j.interrupt(1,'runtime_closed')
        self.assertEqual(2,len(j.batch()));self.assertEqual(0,len(j.pending))
        new=PostActionJournal('next')
        with self.assertRaises(ValueError):new.accept_receipt(prepared,receipt(plan))

    def test_capacity_checked_before_physical_mutation_and_ack_keeps_counts(self):
        j=PostActionJournal('r',capacity=1);p,s,o,plan=context();j.accept_receipt(self.prepare(j,p,s,o,plan),receipt(plan))
        with self.assertRaises(BufferError):self.prepare(j,p,s,o,plan)
        self.advance(j,o,s);batch=j.batch();j.acknowledge(batch)
        self.assertEqual(0,len(j.batch()));self.assertEqual(1,j.counts['completed'])
        self.assertEqual(1,len(j.recent))

    def test_recorder_append_failure_does_not_acknowledge_or_leave_partial_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Path(__file__).parent/'scenarios/unidirectional_corridor/scenario.sumocfg'
            rec=ExperimentRecorder('qa',cfg,{},root=Path(tmp))
            j=PostActionJournal('r');p,s,o,plan=context();j.accept_receipt(self.prepare(j,p,s,o,plan),receipt(plan,'rejected'))
            batch=j.batch();rec.record_post_action(batch);j.acknowledge(batch)
            rows=[json.loads(x) for x in (rec.directory/'post_action.jsonl').read_text().splitlines()]
            self.assertEqual(1,len(rows));self.assertEqual(rows[0],batch[0].as_dict())
            j2=PostActionJournal('r');j2.accept_receipt(self.prepare(j2,p,s,o,plan),receipt(plan,'rejected'))
            before=(rec.directory/'post_action.jsonl').read_bytes()
            real_open=Path.open
            class FailingAppend:
                def __enter__(self):
                    self.handle=real_open(rec.directory/'post_action.jsonl','a+',encoding='utf-8');return self
                def __exit__(self,*args):self.handle.close()
                def write(self,value):
                    self.handle.write(value[:10]);raise OSError('qa partial append failure')
                def __getattr__(self,key):return getattr(self.handle,key)
            with patch.object(Path,'open',return_value=FailingAppend()):
                with self.assertRaisesRegex(OSError,'partial append'):
                    rec.record_post_action(j2.batch())
            self.assertEqual(before,(rec.directory/'post_action.jsonl').read_bytes())
            self.assertEqual(1,len(j2.batch()))
            rec.decision_writer.close()

    def test_real_sumo_timeline_truncates_observed_record_not_goal_success(self):
        import crowdsim.core.simulation_runtime as rm
        from functools import partial
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='qa-post-truncated-') as tmp:
            with patch.object(rm,'PROJECT_ROOT',Path(tmp)),patch.object(rm,'ExperimentRecorder',partial(ExperimentRecorder,root=Path(tmp)/'runs')):
                rt=SimulationRuntime(root/'tests/scenarios/alternative_routes/scenario.sumocfg',pedestrian_route_files=[root/'tests/scenarios/alternative_routes/demand.rou.xml'],sumo_binary=str(root/'.venv/sumo-1.24.0/bin/sumo'),demand_mode='fixed')
                try:
                    rt.initialize();rt.start();rt.tick()
                    rt.decision_engine=engine();rt.decision_scheduler.engine=rt.decision_engine
                    pid=next(iter(rt.current.persons));rt.population.profiles[pid]=replace(rt.population.profiles[pid],crowd_role='consumer')
                    rt.timeline_end_seconds=rt.time_seconds+rt.step_length
                    rt.apply_plan(BehaviorPlan(pid,rt.snapshot_id,'wait',wait_until=rt.time_seconds+3,decided_at=rt.time_seconds))
                    rt._step_after_plans([])
                    row=rt.post_action_journal.recent[-1].as_dict()
                    self.assertEqual(RuntimeState.FINISHED,rt.state)
                    self.assertTrue(row['truncated']);self.assertFalse(row['terminated'])
                    self.assertEqual('timeline_end',row['end_reason']);self.assertIsNotNone(row['after'])
                    self.assertIsNone(row['reward']);self.assertFalse(row['trainable'])
                finally:rt.close()

    def test_real_sumo_records_only_learning_roles_and_reset_discards_pending(self):
        import crowdsim.core.simulation_runtime as rm
        from functools import partial
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='qa-post-action-') as tmp:
            with patch.object(rm,'PROJECT_ROOT',Path(tmp)),patch.object(rm,'ExperimentRecorder',partial(ExperimentRecorder,root=Path(tmp)/'runs')):
                rt=SimulationRuntime(root/'tests/scenarios/alternative_routes/scenario.sumocfg',pedestrian_route_files=[root/'tests/scenarios/alternative_routes/demand.rou.xml'],sumo_binary=str(root/'.venv/sumo-1.24.0/bin/sumo'),demand_mode='fixed')
                try:
                    rt.initialize();rt.start();rt.tick()
                    rt.decision_engine=engine();rt.decision_scheduler.engine=rt.decision_engine
                    pid=next(iter(rt.current.persons));rt.population.profiles[pid]=replace(rt.population.profiles[pid],crowd_role='consumer')
                    plan=BehaviorPlan(pid,rt.snapshot_id,'wait',wait_until=rt.time_seconds+3,decided_at=rt.time_seconds)
                    before=rt.population.states[pid].planned_wait_until
                    self.assertEqual('applied',rt.apply_plan(plan).status)
                    rt._step_after_plans([])
                    row=rt.post_action_journal.recent[-1].as_dict()
                    self.assertEqual(before,row['before']['state']['planned_wait_until'])
                    self.assertIsNotNone(row['after']);self.assertIsNone(row['reward'])
                    self.assertEqual(rt.snapshot_id,row['after_snapshot_id'])
                    self.assertEqual(rt.current.persons[pid].speed,row['after']['observation']['own_motion']['speed'])
                    rt.population.profiles[pid]=replace(rt.population.profiles[pid],crowd_role='commuter')
                    n=rt.post_action_journal.counts['receipts']
                    rt.apply_plan(replace(plan,snapshot_id=rt.snapshot_id,decided_at=rt.time_seconds,wait_until=rt.time_seconds+3))
                    self.assertEqual(n,rt.post_action_journal.counts['receipts'])
                    rt.population.profiles[pid]=replace(rt.population.profiles[pid],crowd_role='consumer')
                    pending_plan=replace(plan,snapshot_id=rt.snapshot_id,decided_at=rt.time_seconds,wait_until=rt.time_seconds+4)
                    rt.apply_plan(pending_plan)
                    replacement=replace(pending_plan,proposed_action='slow_down',wait_until=None,speed_limit=.4)
                    with patch.object(rt.recorder,'record_post_action',side_effect=OSError('qa disk failure')):
                        with self.assertRaisesRegex(OSError,'qa disk failure'):rt.apply_plan(replacement)
                    self.assertEqual(replacement,rt.population.states[pid].current_plan)
                    self.assertTrue(rt.post_action_journal.batch())
                    rt._flush_post_action()
                    journal=rt.post_action_journal;old_dir=rt.recorder.directory
                    rt.reset()
                    self.assertIsNot(journal,rt.post_action_journal);self.assertEqual(0,len(rt.post_action_journal.pending))
                    self.assertEqual(0,len(journal.pending))
                    rows=[json.loads(x) for x in (old_dir/'post_action.jsonl').read_text().splitlines()]
                    self.assertEqual('runtime_closed',rows[-1]['end_reason'])
                    self.assertTrue(rows[-1]['truncated'])
                finally:rt.close()

if __name__=='__main__':unittest.main()
