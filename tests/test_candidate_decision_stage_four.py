"""Unique-candidate selection and no late-batch side effects, offline by default."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from crowdsim.decision.route_provider import RouteCandidate, bind_candidates, select_candidate, RouteProvider
from crowdsim.decision.decision_scheduler import DecisionScheduler
from crowdsim.decision.plan_executor import PlanExecutor
from crowdsim.core.simulation_runtime import SimulationRuntime, RuntimeState
from crowdsim.modeling_algorithm.contracts import GameChoice, PADState, DesireState
from crowdsim.modeling_algorithm.individual_model.pipeline import IndividualModelingEngine
from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
from crowdsim.modeling_algorithm.individual_model.game.bounded_rationality import BoundedRationalGameModel
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, Observation, BehaviorPlan
from tests.test_modeling_binding_stage_one import compiled_model
import tests.test_poi_replanning as poi
from tests.test_decision_scheduler import inputs


def engine(llm=False):
    agents=compiled_model()['individual_model']['agent_types']
    if llm:
        a=next(a for a in agents if a['id']=='consumer')
        next(m for m in a['architecture'] if m['id']=='decision')['method']='LLM-driven'
    e=IndividualModelingEngine()
    e.configure(agents,execution_contract='candidate-decision-v1')
    if llm:
        # Explicit key-less local QA protocol mode; the request is still mocked in
        # these tests, this is not a real external upstream.
        for assembly in e.assemblies.values():
            if assembly.decision is not None:
                assembly.decision.qa_local=True
    return e


def candidates(person='p',snapshot='s'):
    return bind_candidates((RouteCandidate('same','c',10,('a','b','c'),20,entry_edge='b'),
                            RouteCandidate('same','c',10,('a','c'),10,entry_edge='c')),person,snapshot)


class CandidateDecisionTests(unittest.TestCase):
    def test_identity_is_unique_reproducible_and_scoped_to_payload_person_boundary(self):
        c=candidates()
        self.assertNotEqual(c[0].candidate_id,c[1].candidate_id)
        self.assertEqual(c,candidates())
        self.assertNotEqual(c[0].candidate_id,candidates(snapshot='next')[0].candidate_id)
        self.assertNotEqual(c[0].candidate_id,candidates(person='other')[0].candidate_id)
        changed=bind_candidates((replace(c[0],estimated_cost_seconds=21),),'p','s')
        self.assertNotEqual(c[0].candidate_id,changed[0].candidate_id)
        with self.assertRaises(ValueError):bind_candidates((c[0],c[0]),'p','s')

    def test_select_second_same_target_and_reject_missing_unknown_duplicate_mismatch(self):
        c=candidates()
        self.assertEqual(c[1],select_candidate(c,candidate_id=c[1].candidate_id,target_id='same'))
        for identity,target in [(None,'same'),('unknown','same'),(c[1].candidate_id,'wrong')]:
            with self.assertRaises(ValueError):select_candidate(c,candidate_id=identity,target_id=target)
        with self.assertRaises(ValueError):select_candidate((c[0],c[0]),candidate_id=c[0].candidate_id)
        self.assertEqual(c[0].target_id,select_candidate(tuple(replace(x,candidate_id=None) for x in c),target_id='same').target_id)

    def test_game_probability_keys_do_not_collide_and_selected_identity_is_retained(self):
        p,s,o=inputs(1);p=p['p00'];s=s['p00'];o=o['p00']
        c=candidates(p.person_id,o.snapshot_id)
        d=DesireState(dict(goal_progress=0,safety=1,avoid_crowding=1,rest=0),'safety')
        choice=BoundedRationalGameModel(rationality=100).choose(p,s,o,PADState(),d,c,{'reroute':10})
        self.assertEqual(5,len(choice.probabilities))
        self.assertAlmostEqual(1,sum(choice.probabilities.values()))
        self.assertIn(choice.candidate_id,[x.candidate_id for x in c])

    def test_pipeline_keeps_second_route_from_game_choice(self):
        e=engine();p=AgentProfile('p',crowd_role='consumer');s=AgentState('p');o=Observation('p','s',1,poi.motion())
        c=candidates();e.assemblies['consumer'].game.choose=Mock(return_value=GameChoice('reroute','same',1,{},'second',c[1].candidate_id))
        plan=e.rule_plan(p,s,o,c)
        self.assertEqual(c[1].edges,plan.route_edges)
        self.assertEqual(c[1].candidate_id,plan.candidate_id)
        s.stress=1
        self.assertEqual(plan,e.rule_plan(p,s,o,()))

    def test_rule_hotspot_plan_retains_exact_second_entrance(self):
        e=engine();p=AgentProfile('p',crowd_role='commuter');s=AgentState('p');o=Observation('p','s',1,poi.motion())
        c=bind_candidates((RouteCandidate('same','c',10,('a','b','c'),30,'hotspot_route',entry_edge='b'),
                           RouteCandidate('same','c',10,('a','c'),10,'hotspot_route',entry_edge='c')),'p','s')
        plan=e.rule_plan(p,s,o,c)
        self.assertEqual(c[1].candidate_id,plan.candidate_id)
        self.assertEqual('c',plan.selected_entry_edge)
        self.assertTrue(plan.preserve_future_stages)

    def test_rule_keeps_identity_when_same_route_has_different_activity_payload(self):
        e=engine();p=AgentProfile('p',crowd_role='commuter')
        s=AgentState('p',current_goal='same');o=Observation('p','s',1,poi.motion())
        c=bind_candidates(tuple(RouteCandidate('same','c',10,('a','c'),10,'activity',
                              activity_duration=d,next_route_edges=('exit',),
                              next_arrival_position=5,next_target_id='leave') for d in (3,7)),'p','s')
        plan=e.rule_plan(p,s,o,c)
        self.assertEqual(c[0].candidate_id,plan.candidate_id)
        self.assertEqual(3,plan.activity_duration)
        self.assertEqual(('exit',),plan.next_route_edges)
        # Reroute to an activity's location does not append its activity stages.
        s=AgentState('p',perceived_risk=1,poi_plan_active=True)
        o=replace(o,snapshot_id='next')
        plan=e.rule_plan(p,s,o,c)
        self.assertEqual('reroute',plan.proposed_action)
        self.assertIsNotNone(plan.candidate_id)
        self.assertIsNone(plan.activity_duration)
        self.assertEqual((),plan.next_route_edges)

    def test_llm_parser_copies_candidate_and_rejects_legacy_target_or_nonroute_identity(self):
        p=AgentProfile('p');o=Observation('p','s',1,poi.motion());c=candidates()
        plan=LLMDrivenDecisionModel._parse_plan({'action':'reroute','candidate_id':c[1].candidate_id},p,o,c,candidate_contract=True)
        self.assertEqual(c[1].edges,plan.route_edges)
        for payload in [{'action':'reroute','target_id':'same'}, {'action':'reroute','candidate_id':'unknown'},
                        {'action':'wait','candidate_id':c[1].candidate_id},
                        {'action':'change_goal','candidate_id':c[1].candidate_id},
                        {'action':'continue','candidate_id':None,'speed':3}]:
            with self.assertRaises(ValueError):LLMDrivenDecisionModel._parse_plan(payload,p,o,c,candidate_contract=True)

    def test_executor_rejects_forged_payload_and_stale_identity_before_traci(self):
        adapter=poi.Adapter();executor=PlanExecutor(adapter,RouteProvider(poi.Network(),adapter));p=AgentProfile('p');o=Observation('p','s',1,poi.motion())
        c=bind_candidates((RouteCandidate('same','c',10,('a','b','c'),20),),'p','s')
        plan=LLMDrivenDecisionModel._make_plan(p,o,'reroute',c[0],reason='x',confidence=None,source='test')
        for bad in [replace(plan,candidate_id='unknown'),replace(plan,arrival_position=9),replace(plan,next_target_id='extra')]:
            result=executor.apply(bad,poi.motion(),'s',1,candidates=c,require_candidate=True)
            self.assertEqual('rejected',result.status)
        self.assertEqual([],adapter.mutations)
        executor.route_retry_after.clear()
        forged=(replace(c[0],arrival_position=9),)
        result=executor.apply(replace(plan,arrival_position=9),poi.motion(),'s',1,candidates=forged,require_candidate=True)
        self.assertEqual('rejected',result.status)
        executor.route_retry_after.clear()
        self.assertEqual('applied',executor.apply(plan,poi.motion(),'s',1,candidates=c,require_candidate=True).status)

    def test_real_sumo_second_entrance_preserves_future_stages(self):
        from crowdsim.infrastructure.sumo_adapter import SumoAdapter
        from crowdsim.infrastructure.network_adapter import ResearchNetwork
        root=Path(__file__).resolve().parents[1]
        adapter=SumoAdapter(root/'tests/scenarios/alternative_routes/scenario.sumocfg',sumo_binary=str(root/'.venv/sumo-1.24.0/bin/sumo'))
        self.addCleanup(adapter.close)
        adapter.start();step=adapter.step();motion=next(iter(step.persons.values()))
        provider=RouteProvider(ResearchNetwork(root/'tests/scenarios/alternative_routes/network.net.xml'),adapter)
        original=adapter.remaining_person_stages(motion.person_id)
        arrival=float(original[0].arrivalPos)
        adapter.append_waiting_stage(motion.person_id,3,'future-wait')
        adapter.append_walking_stage(motion.person_id,('exit',),arrival)
        before=adapter.remaining_person_stages(motion.person_id)
        raw=[provider.build_via_candidate(motion,target_id='same',target_edge='exit',via_edge=via,arrival_position=arrival) for via in ('upper_a','lower_a')]
        c=bind_candidates(raw,motion.person_id,'s')
        o=Observation(motion.person_id,'s',step.time_seconds,motion)
        plan=LLMDrivenDecisionModel._parse_plan({'action':'reroute','candidate_id':c[1].candidate_id},AgentProfile(motion.person_id),o,c,candidate_contract=True)
        executor=PlanExecutor(adapter,provider)
        self.assertEqual('applied',executor.apply(plan,motion,'s',step.time_seconds,candidates=c,require_candidate=True).status)
        after=adapter.remaining_person_stages(motion.person_id)
        self.assertEqual(c[1].edges,tuple(after[0].edges))
        self.assertIn('lower_a',after[0].edges)
        self.assertNotIn('upper_a',after[0].edges)
        self.assertEqual([(x.type,tuple(x.edges),x.arrivalPos) for x in before[1:]],[(x.type,tuple(x.edges),x.arrivalPos) for x in after[1:]])
        self.assertEqual(arrival,float(after[0].arrivalPos))
        adapter.step();adapter.close();self.assertTrue(adapter.closed)


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduling_source_is_consistent_in_rule_and_budget_paths(self):
        e=engine(llm=True);p,s,o=inputs(2)
        p={pid:replace(v,crowd_role='consumer') for pid,v in p.items()}
        e.assemblies['consumer'].decision._request=AsyncMock(return_value={'action':'continue','candidate_id':None})
        scheduler=DecisionScheduler(e,llm_budget=1)
        plans=await scheduler.resolve(list(p),p,s,o,use_llm=True)
        self.assertEqual(['llm_request','budget_fallback'],[v.scheduling_source for v in plans])
        self.assertEqual({v.person_id:v.scheduling_source for v in plans},scheduler.last_sources)
        obs={pid:replace(v,snapshot_id='next',time_seconds=4) for pid,v in o.items()}
        plans=scheduler.resolve_rule(iter(p),p,s,obs)
        self.assertTrue(all(v.scheduling_source=='configured_rule' for v in plans))
        self.assertEqual(dict.fromkeys(p,'configured_rule'),scheduler.last_sources)

    async def test_internal_fallback_keeps_fallback_source_and_is_not_llm_success(self):
        e=engine(llm=True);p,s,o=inputs(1)
        p={pid:replace(v,crowd_role='consumer') for pid,v in p.items()}
        e.assemblies['consumer'].decision._request=AsyncMock(side_effect=RuntimeError('qa-offline'))
        scheduler=DecisionScheduler(e,llm_budget=1)
        plans=await scheduler.resolve(list(p),p,s,o,use_llm=True)
        plan=plans[0]
        self.assertEqual('bounded_rational_fallback',plan.source)
        self.assertEqual('bounded_rational_fallback',plan.scheduling_source)
        self.assertEqual(1,scheduler.llm_requests)
        self.assertEqual(0,scheduler.llm_count)
        self.assertEqual(1,scheduler.llm_fallbacks)

    async def test_twelve_basic_people_do_not_consume_one_llm_slot(self):
        e=engine(llm=True);p,s,o=inputs(13)
        p={pid:replace(v,crowd_role='commuter' if pid!='p12' else 'consumer') for pid,v in p.items()}
        e.assemblies['consumer'].decision._request=AsyncMock(return_value={'action':'continue','candidate_id':None})
        scheduler=DecisionScheduler(e,llm_budget=1)
        plans=await scheduler.resolve(list(p),p,s,o,use_llm=True)
        self.assertEqual(13,len(plans));self.assertEqual(1,e.assemblies['consumer'].decision._request.await_count)
        self.assertEqual(0,scheduler.budget_fallback_count)
        self.assertEqual(12,scheduler.rule_count)

    async def test_eligible_rotation_does_not_starve_after_rule_fallback(self):
        e=engine(llm=True);p,s,o=inputs(3);p={pid:replace(v,crowd_role='consumer') for pid,v in p.items()}
        called=[]
        async def request(messages):
            import json
            called.append(json.loads(messages[1]['content'])['person_id'])
            return {'action':'continue','candidate_id':None}
        e.assemblies['consumer'].decision._request=request
        scheduler=DecisionScheduler(e,llm_budget=1)
        for i in range(3):
            obs={pid:replace(v,snapshot_id=f's{i}',time_seconds=i+1) for pid,v in o.items()}
            await scheduler.resolve(list(p),p,s,obs,use_llm=True)
        self.assertEqual(set(p),set(called))

    async def test_invalidated_batch_changes_no_deadline_or_rotation(self):
        e=engine(llm=True);p,s,o=inputs(1);p={pid:replace(v,crowd_role='consumer') for pid,v in p.items()}
        began=asyncio.Event();release=asyncio.Event()
        async def delayed(messages):
            began.set();await release.wait();return {'action':'continue','candidate_id':None}
        e.assemblies['consumer'].decision._request=delayed
        scheduler=DecisionScheduler(e,llm_budget=1)
        task=asyncio.create_task(scheduler.resolve(list(p),p,s,o,use_llm=True))
        await began.wait();scheduler.invalidate();release.set()
        self.assertEqual([],await task)
        self.assertEqual(0,s['p00'].next_decision_time);self.assertEqual([],list(scheduler.rotation))
        self.assertEqual(1,scheduler.discarded_batches)

    async def test_changed_snapshot_is_discarded(self):
        e=engine(llm=True);p,s,o=inputs(1);p={pid:replace(v,crowd_role='consumer') for pid,v in p.items()}
        began=asyncio.Event();release=asyncio.Event()
        async def delayed(messages):
            began.set();await release.wait();return {'action':'continue','candidate_id':None}
        e.assemblies['consumer'].decision._request=delayed
        scheduler=DecisionScheduler(e)
        task=asyncio.create_task(scheduler.resolve(list(p),p,s,o,use_llm=True))
        await began.wait();o['p00']=replace(o['p00'],snapshot_id='next');release.set()
        self.assertEqual([],await task);self.assertEqual(0,s['p00'].next_decision_time)

    async def test_circuit_breaker_role_does_not_consume_model_budget(self):
        import time
        e=engine(llm=True);p,s,o=inputs(1);p={pid:replace(v,crowd_role='consumer') for pid,v in p.items()}
        decision=e.assemblies['consumer'].decision
        decision._unavailable_until=time.monotonic()+30
        decision._request=AsyncMock(side_effect=AssertionError('breaker must not request'))
        scheduler=DecisionScheduler(e,llm_budget=1)
        plans=await scheduler.resolve(list(p),p,s,o,use_llm=True)
        self.assertEqual(1,len(plans));decision._request.assert_not_awaited()
        self.assertEqual(0,scheduler.budget_fallback_count)

    async def test_changed_state_or_candidates_do_not_update_schedule(self):
        for kind in ('state','candidates','profile'):
            e=engine(llm=True);p,s,o=inputs(1);p={pid:replace(v,crowd_role='consumer') for pid,v in p.items()}
            began=asyncio.Event();release=asyncio.Event();cs={'p00':()}
            async def delayed(messages):
                began.set();await release.wait();return {'action':'continue','candidate_id':None}
            e.assemblies['consumer'].decision._request=delayed
            scheduler=DecisionScheduler(e)
            task=asyncio.create_task(scheduler.resolve(list(p),p,s,o,use_llm=True,candidates=cs))
            await asyncio.wait_for(began.wait(),2)
            if kind=='state':s['p00'].stress=1
            elif kind=='profile':p['p00']=replace(p['p00'],patience=.001)
            else:cs['p00']=candidates('p00','s')
            release.set()
            self.assertEqual([],await task);self.assertEqual(0,s['p00'].next_decision_time)
            self.assertEqual(0,e.diagnostics()['llm_decisions'])
            self.assertEqual(0,e.pipeline_decisions)

    async def test_cancellation_does_not_mark_scheduled(self):
        e=engine(llm=True);p,s,o=inputs(1);p={pid:replace(v,crowd_role='consumer') for pid,v in p.items()}
        began=asyncio.Event()
        async def delayed(messages):
            began.set();await asyncio.Event().wait()
        e.assemblies['consumer'].decision._request=delayed
        scheduler=DecisionScheduler(e)
        task=asyncio.create_task(scheduler.resolve(list(p),p,s,o,use_llm=True))
        await asyncio.wait_for(began.wait(),2);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(0,s['p00'].next_decision_time)
        self.assertEqual(0,e.diagnostics()['llm_decisions'])
        self.assertEqual([],list(scheduler.rotation))

    async def test_real_runtime_reset_rejects_late_old_model_response(self):
        import tempfile
        import crowdsim.core.simulation_runtime as runtime_module
        from functools import partial
        from unittest.mock import patch
        from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='candidate-reset-') as tmp:
            output=Path(tmp)
            with patch.object(runtime_module,'PROJECT_ROOT',output), patch.object(runtime_module,'ExperimentRecorder',partial(ExperimentRecorder,root=output/'runs')):
                runtime=SimulationRuntime(root/'tests/scenarios/alternative_routes/scenario.sumocfg',pedestrian_route_files=[root/'tests/scenarios/alternative_routes/demand.rou.xml'],sumo_binary=str(root/'.venv/sumo-1.24.0/bin/sumo'),demand_mode='fixed')
                try:
                    runtime.initialize();runtime.start();runtime.tick()
                    e=engine(llm=True);runtime.decision_engine=e;runtime.decision_scheduler=DecisionScheduler(e);runtime.use_llm=True
                    runtime.population.profiles={pid:replace(profile,crowd_role='consumer') for pid,profile in runtime.population.profiles.items()}
                    for state in runtime.population.states.values():state.next_decision_time=0
                    began=asyncio.Event();release=asyncio.Event()
                    async def delayed(messages):
                        began.set();await release.wait();return {'action':'continue','candidate_id':None}
                    e.assemblies['consumer'].decision._request=delayed
                    task=asyncio.create_task(runtime.tick_async())
                    await asyncio.wait_for(began.wait(),2)
                    old_run=runtime.run_id
                    runtime.reset();runtime.start()
                    new_snapshot=runtime.snapshot_id
                    release.set();await asyncio.wait_for(task,2)
                    self.assertNotEqual(old_run,runtime.run_id)
                    self.assertEqual(new_snapshot,runtime.snapshot_id)
                    self.assertEqual(RuntimeState.RUNNING,runtime.state)
                    self.assertEqual(0,e.diagnostics()['llm_decisions'])
                    self.assertEqual(0,e.pipeline_decisions)
                    self.assertIsNone(runtime._async_tick_inflight)
                finally:
                    runtime.close()

    async def test_pause_resume_while_request_inflight_never_steps(self):
        runtime=SimulationRuntime.__new__(SimulationRuntime)
        runtime.state=RuntimeState.RUNNING;runtime.run_id='r';runtime.snapshot_index=0;runtime._decision_epoch='epoch'
        runtime._async_tick_inflight=None;runtime.discarded_decision_batches=0
        p,s,o=inputs(1);p={pid:replace(v,crowd_role='consumer') for pid,v in p.items()}
        runtime.population=SimpleNamespace(profiles=p,states=s);runtime.observations=o;runtime.decision_candidates={}
        runtime.use_llm=True;runtime.current=object();runtime.performance=SimpleNamespace(measure=lambda *a:nullcontext())
        e=engine(llm=True);runtime.decision_scheduler=DecisionScheduler(e)
        runtime._prepare_boundary=lambda:list(p)
        runtime._step_after_plans=Mock(side_effect=AssertionError('must not step stale batch'))
        began=asyncio.Event();release=asyncio.Event()
        async def delayed(messages):
            began.set();await release.wait();return {'action':'continue','candidate_id':None}
        e.assemblies['consumer'].decision._request=delayed
        task=asyncio.create_task(runtime.tick_async());await began.wait()
        runtime.pause();runtime.state=RuntimeState.RUNNING
        release.set();self.assertIs(runtime.current,await task)
        runtime._step_after_plans.assert_not_called();self.assertEqual(0,s['p00'].next_decision_time)
        self.assertIsNone(runtime._async_tick_inflight)


if __name__=='__main__':unittest.main()
