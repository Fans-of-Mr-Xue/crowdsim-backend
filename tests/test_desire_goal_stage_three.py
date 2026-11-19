"""Causal goal preparation, frozen affect and protected itinerary counterexamples."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock
from crowdsim.modeling_algorithm.contracts import DesireState, PADState
from crowdsim.modeling_algorithm.individual_model.pipeline import IndividualModelingEngine
from crowdsim.modeling_algorithm.individual_model.desire_goal.runtime_goal import goal_intent
from crowdsim.modeling_algorithm.runtime_binding import build_runtime_binding
from crowdsim.domain.artificial_society_spec import ArtificialSocietySpec
from crowdsim.decision.agent_decision import AgentDecisionEngine
from tests.test_modeling_binding_stage_one import compiled_model
from crowdsim.domain.crowdsim_models import AgentProfile
import tests.test_poi_replanning as poi_tests


def engine():
    model=compiled_model()
    model['individual_model']['execution_contract']='desire-goal-v1'
    model['runtime_binding']=build_runtime_binding(model['individual_model'])
    ArtificialSocietySpec.parse(model)
    result=IndividualModelingEngine(AgentDecisionEngine())
    result.configure(model['individual_model']['agent_types'],execution_contract='desire-goal-v1')
    return result


class DesireGoalTests(unittest.TestCase):
    def runtime(self):
        helper=poi_tests.RuntimeContextTests();helper.setUp()
        runtime=helper.runtime
        runtime.decision_engine=engine()
        runtime.population.profile_for=lambda pid: replace(AgentProfile(pid),crowd_role='leisure_visitor')
        runtime.decision_engine.assemblies['leisure_visitor'].emotion.update=Mock(return_value=PADState(-1,1,-1))
        return runtime

    def set_desire(self,runtime,primary):
        scores={key:0.0 for key in ('safety','avoid_crowding','rest','goal_progress','group_cohesion','follow_guidance')}
        scores[primary]=1.0
        runtime.decision_engine.assemblies['leisure_visitor'].desire.generate=Mock(return_value=DesireState(scores,primary))

    def test_safety_precedes_route_queries_and_changes_goal_and_executable_route(self):
        runtime=self.runtime();self.set_desire(runtime,'safety')
        runtime._refresh_agent_context(['p'])
        self.assertEqual('exit',runtime.population.states['p'].current_goal)
        candidates=runtime.decision_candidates['p']
        self.assertEqual('exit',candidates[0].target_id)
        plan=runtime.decision_engine.rule_plan(runtime.population.profile_for('p'),runtime.population.states['p'],runtime.observations['p'],candidates)
        self.assertEqual('reroute',plan.proposed_action)
        self.assertEqual('exit',plan.target_id)
        self.assertEqual(candidates[0].edges,plan.route_edges)
        self.assertEqual(1,runtime.decision_engine.assemblies['leisure_visitor'].emotion.update.call_count)
        self.assertEqual(1,runtime.decision_engine.assemblies['leisure_visitor'].desire.generate.call_count)

    def test_goal_progress_control_retains_activity_in_same_observation(self):
        runtime=self.runtime();self.set_desire(runtime,'goal_progress')
        runtime._refresh_agent_context(['p'])
        self.assertEqual('museum',runtime.population.states['p'].current_goal)
        plan=runtime.decision_engine.rule_plan(runtime.population.profile_for('p'),runtime.population.states['p'],runtime.observations['p'],runtime.decision_candidates['p'])
        self.assertEqual('change_goal',plan.proposed_action)
        self.assertEqual('museum',plan.target_id)
        self.assertTrue(plan.next_route_edges)

    def test_rest_does_not_invent_facility_or_change_goal(self):
        runtime=self.runtime();self.set_desire(runtime,'rest')
        runtime._refresh_agent_context(['p'])
        self.assertEqual('museum',runtime.population.states['p'].current_goal)
        plan=runtime.decision_engine.rule_plan(runtime.population.profile_for('p'),runtime.population.states['p'],runtime.observations['p'],runtime.decision_candidates['p'])
        self.assertEqual('wait',plan.proposed_action)
        self.assertEqual((),plan.route_edges)
        self.assertIsNone(plan.target_id)

    def test_unreachable_intention_never_becomes_available_route(self):
        runtime=self.runtime();self.set_desire(runtime,'safety')
        runtime.poi_catalog.pois['exit']['edge']='island'
        runtime._refresh_agent_context(['p'])
        self.assertEqual((),runtime.decision_candidates['p'])
        self.assertEqual((),runtime.observations['p'].available_goal_ids)
        self.assertIn('exit',runtime.decision_engine.diagnostics()['goal_contexts']['p']['route_failures'])

    def test_locked_itinerary_is_not_replanned_or_overridden_by_rest(self):
        runtime=self.runtime();self.set_desire(runtime,'rest')
        runtime.population.locked_itinerary_ids.add('p')
        runtime._refresh_agent_context(['p'])
        self.assertEqual('museum',runtime.population.states['p'].current_goal)
        self.assertTrue(runtime.decision_engine.diagnostics()['goal_contexts']['p']['locked'])
        plan=runtime.decision_engine.rule_plan(runtime.population.profile_for('p'),runtime.population.states['p'],runtime.observations['p'])
        self.assertEqual('continue',plan.proposed_action)

    def test_frozen_candidates_and_state_survive_repeated_boundary_and_clear(self):
        runtime=self.runtime();self.set_desire(runtime,'safety')
        runtime._refresh_agent_context(['p'])
        p=runtime.population.profile_for('p');s=runtime.population.states['p'];o=runtime.observations['p']
        first=runtime.decision_engine.rule_plan(p,s,o,runtime.decision_candidates['p'])
        s.current_goal='museum'
        calls=runtime.decision_candidates['p']
        runtime.route_provider.build_activity_candidate=Mock(side_effect=AssertionError('same boundary must not regenerate routes'))
        runtime._refresh_agent_context(['p'])
        self.assertEqual(calls,runtime.decision_candidates['p'])
        self.assertEqual(first,runtime.decision_engine.rule_plan(p,s,o,()))
        runtime.decision_engine.retain_active_people([])
        self.assertEqual({},runtime.decision_engine.diagnostics()['goal_contexts'])

    def test_legacy_and_nonparticipating_roles_do_not_run_goal_desire(self):
        runtime=self.runtime();self.set_desire(runtime,'safety')
        agents=compiled_model()['individual_model']['agent_types']
        runtime.decision_engine.configure(agents,execution_contract='module-assembly-v1')
        runtime._refresh_agent_context(['p'])
        self.assertEqual('museum',runtime.population.states['p'].current_goal)
        self.assertEqual({},runtime.decision_engine.diagnostics()['goal_contexts'])
        runtime.decision_engine=engine()
        runtime.population.profile_for=lambda pid: replace(AgentProfile(pid),crowd_role='consumer')
        runtime._refresh_agent_context(['p'])
        self.assertEqual({},runtime.decision_engine.diagnostics()['goal_contexts'])

    def test_real_pad_parameter_changes_goal_with_identical_physical_observation(self):
        from crowdsim.modeling_algorithm.individual_model.emotion.pad_continuous import PADContinuousEmotionModel
        outcomes=[]
        for inertia in (0.0,0.95):
            runtime=self.runtime()
            profile=AgentProfile('p',crowd_role='leisure_visitor',familiarity=1,endurance=1,
                                 group_cohesion=0,following_tendency=0,authority_compliance=0)
            runtime.population.profile_for=lambda pid: profile
            runtime.decision_engine.assemblies['leisure_visitor'].emotion=PADContinuousEmotionModel(inertia=inertia)
            runtime.decision_engine.emotional_states['p']=PADState(-1,1,-1)
            runtime._refresh_agent_context(['p'])
            diagnostics=runtime.decision_engine.diagnostics()['goal_contexts']['p']
            plan=runtime.decision_engine.rule_plan(profile,runtime.population.states['p'],runtime.observations['p'],runtime.decision_candidates['p'])
            execution=runtime.plan_executor.apply(plan,runtime.observations['p'].own_motion,'s',1)
            self.assertEqual('applied',execution.status)
            outcomes.append((diagnostics['primary'],plan.target_id,plan.proposed_action))
        self.assertEqual([('goal_progress','museum','change_goal'),('safety','exit','reroute')],outcomes)

    def test_real_sumo_accepts_causal_routes_without_any_position_generation(self):
        from pathlib import Path
        from crowdsim.infrastructure.sumo_adapter import SumoAdapter
        from crowdsim.infrastructure.network_adapter import ResearchNetwork
        from crowdsim.decision.route_provider import RouteProvider
        from crowdsim.decision.plan_executor import PlanExecutor
        from crowdsim.domain.crowdsim_models import AgentState, Observation
        from crowdsim.modeling_algorithm.individual_model.emotion.pad_continuous import PADContinuousEmotionModel
        root=Path(__file__).resolve().parents[1]
        adapter=SumoAdapter(root/'tests/scenarios/alternative_routes/scenario.sumocfg',sumo_binary=str(root/'.venv/sumo-1.24.0/bin/sumo'))
        self.addCleanup(adapter.close)
        adapter.start()
        step=adapter.step()
        motion=next(iter(step.persons.values()))
        provider=RouteProvider(ResearchNetwork(root/'tests/scenarios/alternative_routes/network.net.xml'),adapter)
        executor=PlanExecutor(adapter,provider)
        targets=({'id':'activity','edge':'upper_b','kind':'activity','stay_seconds':[2,2]},
                 {'id':'exit','edge':'exit','kind':'exit'})
        observation=Observation(motion.person_id,'causal-sumo',step.time_seconds,motion)
        outcomes=[]
        for inertia in (0,.95):
            e=engine()
            profile=AgentProfile(motion.person_id,crowd_role='leisure_visitor',familiarity=1,endurance=1,group_cohesion=0,following_tendency=0,authority_compliance=0)
            state=AgentState(motion.person_id,current_goal='activity',activity_plan=['activity','exit'])
            e.assemblies['leisure_visitor'].emotion=PADContinuousEmotionModel(inertia=inertia)
            e.emotional_states[motion.person_id]=PADState(-1,1,-1)
            intent=e.prepare_goal_intent(profile,state,observation,targets)
            ordered=[next(t for t in targets if t['id']==tid) for tid in intent.target_ids]
            candidates=tuple(provider.build_activity_candidate(motion,t,targets[1] if t['kind']=='activity' else None) for t in ordered)
            state.current_goal=candidates[0].target_id
            e.finalize_goal_context(profile,state,observation,candidates)
            plan=e.rule_plan(profile,state,observation,candidates)
            executor.register_profile(profile)
            result=executor.apply(plan,motion,observation.snapshot_id,step.time_seconds)
            self.assertEqual('applied',result.status)
            stages=adapter.remaining_person_stages(motion.person_id)
            self.assertEqual(plan.route_edges,tuple(stages[0].edges))
            self.assertEqual(3 if plan.proposed_action=='change_goal' else 1,len(stages))
            outcomes.append((intent.primary,plan.target_id))
        self.assertEqual([('goal_progress','activity'),('safety','exit')],outcomes)
        next_step=adapter.step()
        self.assertIn(motion.person_id,next_step.persons)
        adapter.close()
        self.assertTrue(adapter.closed)
        self.assertIsNone(adapter.close_error)

    def test_missing_guidance_target_and_active_activity_keep_existing_goal(self):
        for mode in ('follow_guidance','group_cohesion'):
            runtime=self.runtime();self.set_desire(runtime,mode)
            runtime._refresh_agent_context(['p'])
            self.assertEqual('museum',runtime.population.states['p'].current_goal)
            self.assertTrue(runtime.decision_engine.goal_trace('p')['locked'])
        runtime=self.runtime();self.set_desire(runtime,'safety')
        runtime.population.states['p'].poi_plan_active=True
        runtime.adapter.stages.append(poi_tests.Stage(type=poi_tests.tc.STAGE_WAITING))
        runtime._refresh_agent_context(['p'])
        self.assertEqual('museum',runtime.population.states['p'].current_goal)
        self.assertTrue(runtime.decision_engine.goal_trace('p')['locked'])

    def test_goal_progress_to_exit_emits_a_real_route_not_only_state_change(self):
        runtime=self.runtime();self.set_desire(runtime,'goal_progress')
        runtime.population.states['p'].activity_plan=['exit']
        runtime.population.states['p'].current_goal='exit'
        runtime._refresh_agent_context(['p'])
        plan=runtime.decision_engine.rule_plan(runtime.population.profile_for('p'),runtime.population.states['p'],runtime.observations['p'],runtime.decision_candidates['p'])
        self.assertEqual('reroute',plan.proposed_action)
        self.assertEqual('exit',plan.target_id)
        self.assertTrue(plan.route_edges)

    def test_nonparticipating_emotion_never_runs_desire_goal(self):
        runtime=self.runtime()
        agents=deepcopy(compiled_model()['individual_model']['agent_types'])
        agent=next(a for a in agents if a['id']=='leisure_visitor')
        agent['emotionState']['participates_in_operation']=False
        agent['architecture']=[m for m in agent['architecture'] if m['id']!='desire']
        runtime.decision_engine.configure(agents,execution_contract='desire-goal-v1')
        runtime._refresh_agent_context(['p'])
        self.assertEqual('museum',runtime.population.states['p'].current_goal)
        self.assertEqual({},runtime.decision_engine.diagnostics()['goal_contexts'])
        self.assertEqual('existing_runtime_hook; no_desire_module',runtime.decision_engine.assemblies['leisure_visitor'].diagnostics()['goal_status'])

    def test_rest_retains_unavailable_goal_instead_of_implicit_reroute(self):
        runtime=self.runtime();self.set_desire(runtime,'rest')
        runtime.population.states['p'].current_goal='closed_activity'
        runtime._refresh_agent_context(['p'])
        self.assertEqual('closed_activity',runtime.population.states['p'].current_goal)
        plan=runtime.decision_engine.rule_plan(runtime.population.profile_for('p'),runtime.population.states['p'],runtime.observations['p'],runtime.decision_candidates['p'])
        self.assertEqual('wait',plan.proposed_action)
        self.assertIsNone(plan.target_id)

    def test_only_known_rendezvous_and_guidance_targets_are_considered(self):
        runtime=self.runtime();s=runtime.population.states['p']
        s.rendezvous_id='invented'
        intent=goal_intent(DesireState({'group_cohesion':1},'group_cohesion'),s,tuple(runtime.poi_catalog.pois.values()))
        self.assertNotIn('invented',intent.target_ids)
        s.rendezvous_id='exit'
        intent=goal_intent(DesireState({'group_cohesion':1},'group_cohesion'),s,tuple(runtime.poi_catalog.pois.values()))
        self.assertEqual('exit',intent.target_ids[0])


    def test_hotspot_target_lock_does_not_disable_emotional_local_action(self):
        from tests.test_modeling_algorithms import context
        from crowdsim.decision.route_provider import RouteCandidate
        profile, state, observation = context(role='leisure_visitor', risk=0, crowding=.9)
        profile = replace(profile, group_cohesion=0, following_tendency=0,
                          authority_compliance=0, familiarity=0, endurance=0)
        state.stress = 1
        state.current_goal = 'hotspot'
        candidate = RouteCandidate('hotspot', 'exit', 0, ('e', 'exit'), 30,
                                   target_kind='hotspot_route', entry_edge='entry')
        e = engine()
        e.prepare_goal_intent(profile, state, observation, (), locked=True, allow_local_actions=True)
        candidates = e.finalize_goal_context(profile, state, observation, (candidate,))
        plan = e.rule_plan(profile, state, observation, candidates)
        self.assertEqual('avoid_crowding', e.goal_trace(profile.person_id)['primary'])
        self.assertEqual('slow_down', plan.proposed_action)
        self.assertIsNone(plan.target_id)
        self.assertEqual((), plan.route_edges)
        self.assertEqual('hotspot', state.current_goal)

    def test_PAD_ablation_changes_hotspot_action_under_identical_observation(self):
        from tests.test_modeling_algorithms import context
        from crowdsim.decision.route_provider import RouteCandidate
        profile, state, observation = context(role='leisure_visitor', risk=0, crowding=.6)
        profile = replace(profile, group_cohesion=0, following_tendency=0,
                          authority_compliance=0, familiarity=0, endurance=0,
                          crowding_tolerance=.95)
        state.stress = 1
        state.current_goal = 'hotspot'
        candidate = RouteCandidate('hotspot', 'exit', 0, ('e', 'exit'), 30,
                                   target_kind='hotspot_route', entry_edge='entry')
        actions = []
        for active in (False, True):
            agents = deepcopy(compiled_model()['individual_model']['agent_types'])
            role = next(a for a in agents if a['id'] == 'leisure_visitor')
            role['emotionState']['participates_in_operation'] = active
            if not active:
                role['architecture'] = [m for m in role['architecture'] if m['id'] != 'desire']
            e = IndividualModelingEngine(AgentDecisionEngine())
            e.configure(agents, execution_contract='candidate-decision-v1')
            intent = e.prepare_goal_intent(profile, state, observation, (), locked=True,
                                          allow_local_actions=True)
            if intent:
                e.finalize_goal_context(profile, state, observation, (candidate,))
            plan = e.rule_plan(profile, state, observation, (candidate,))
            actions.append(plan.proposed_action)
            self.assertIn(profile.person_id, e.emotional_states)
        self.assertEqual(['reroute', 'slow_down'], actions)

    def test_hotspot_local_desire_does_not_require_an_available_route(self):
        from tests.test_modeling_algorithms import context
        profile, state, observation = context(role='leisure_visitor', risk=0, crowding=0,
                                               stress=0, fatigue=1)
        profile = replace(profile, group_cohesion=0, following_tendency=0,
                          authority_compliance=0, familiarity=0, endurance=0, patience=0)
        state.current_goal = 'hotspot'
        state.blocked_duration = 30
        e = engine()
        intent = e.prepare_goal_intent(profile, state, observation, (), locked=True,
                                      allow_local_actions=True)
        self.assertEqual('rest', intent.primary)
        self.assertEqual('wait', intent.local_action)
        e.finalize_goal_context(profile, state, observation, ())
        plan = e.rule_plan(profile, state, observation, ())
        self.assertEqual('wait', plan.proposed_action)
        self.assertIsNone(plan.target_id)
        self.assertEqual('hotspot', state.current_goal)

    def test_hotspot_active_activity_and_fixed_itinerary_remain_protected(self):
        from tests.test_modeling_algorithms import context
        from crowdsim.decision.route_provider import RouteCandidate
        for mode in ('active_activity', 'fixed_itinerary'):
            profile, state, observation = context(role='leisure_visitor', risk=0, crowding=.9)
            profile = replace(profile, group_cohesion=0, following_tendency=0,
                              authority_compliance=0, familiarity=0, endurance=0)
            state.current_goal = 'hotspot'
            state.poi_plan_active = mode == 'active_activity'
            state.activity_state = 'hotspot_dwelling' if state.poi_plan_active else 'walking'
            e = engine()
            e.prepare_goal_intent(profile, state, observation, (), locked=True,
                                  allow_local_actions=mode == 'active_activity')
            e.finalize_goal_context(profile, state, observation, ())
            plan = e.rule_plan(profile, state, observation, ())
            # Existing physical crowd rule may slow a walking person, but no new
            # emotional override can replace the protected destination or hold.
            self.assertNotIn('emotional desire', plan.reason)
            self.assertNotIn(plan.proposed_action, {'reroute', 'change_goal'})
            self.assertEqual('hotspot', state.current_goal)

    def test_real_sumo_hotspot_emotional_action_preserves_all_stages(self):
        from pathlib import Path
        from crowdsim.infrastructure.sumo_adapter import SumoAdapter
        from crowdsim.infrastructure.network_adapter import ResearchNetwork
        from crowdsim.decision.route_provider import RouteProvider
        from crowdsim.decision.plan_executor import PlanExecutor
        from crowdsim.domain.crowdsim_models import AgentState, Observation
        root = Path(__file__).resolve().parents[1]
        adapter = SumoAdapter(root/'tests/scenarios/alternative_routes/scenario.sumocfg',
                              sumo_binary=str(root/'.venv/sumo-1.24.0/bin/sumo'))
        self.addCleanup(adapter.close)
        adapter.start()
        step = adapter.step()
        motion = next(iter(step.persons.values()))
        provider = RouteProvider(ResearchNetwork(root/'tests/scenarios/alternative_routes/network.net.xml'), adapter)
        executor = PlanExecutor(adapter, provider)
        candidate = replace(provider.build_candidate(motion, target_id='hotspot', target_edge='exit'),
                            target_kind='hotspot_route', entry_edge='entry')
        profile = AgentProfile(motion.person_id, crowd_role='leisure_visitor', familiarity=0,
                               endurance=0, group_cohesion=0, following_tendency=0,
                               authority_compliance=0, crowding_tolerance=.95)
        state = AgentState(motion.person_id, current_goal='hotspot', stress=1,
                           perceived_crowding=.6)
        observation = Observation(motion.person_id, 'physical-hotspot', step.time_seconds, motion,
                                  perceived_crowding=.6)
        stages = [(s.type, tuple(s.edges), s.arrivalPos) for s in adapter.remaining_person_stages(motion.person_id)]
        e = engine()
        e.prepare_goal_intent(profile, state, observation, (), locked=True, allow_local_actions=True)
        e.finalize_goal_context(profile, state, observation, (candidate,))
        plan = e.rule_plan(profile, state, observation, (candidate,))
        self.assertEqual('slow_down', plan.proposed_action)
        executor.register_profile(profile)
        result = executor.apply(plan, motion, observation.snapshot_id, step.time_seconds)
        self.assertEqual('applied', result.status)
        self.assertEqual('slow_down', result.applied_action)
        after = [(s.type, tuple(s.edges), s.arrivalPos) for s in adapter.remaining_person_stages(motion.person_id)]
        self.assertEqual(stages, after)
        self.assertEqual('hotspot', state.current_goal)
        self.assertIn(motion.person_id, adapter.step().persons)


if __name__=='__main__':unittest.main()
