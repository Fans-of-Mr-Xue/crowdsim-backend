"""Opt-in module assembly checks; no SUMO, HTTP or formal storage."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock, AsyncMock
import asyncio

from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.modeling_algorithm.individual_model.pipeline import IndividualModelingEngine
from crowdsim.modeling_algorithm.compiler import compile_artificial_society
from tests.test_modeling_binding_stage_one import compiled_model
from tests.test_modeling_algorithms import context
from tests.test_modeling_requirements_compiler import requirement_payload, configured_modeling, confirm_modeling
from crowdsim.domain.artificial_society_spec import ArtificialSocietySpec


class ModuleAssemblyTests(unittest.TestCase):
    def model(self):
        model = compiled_model()
        model['individual_model']['execution_contract'] = 'module-assembly-v1'
        return model

    def configure(self, model=None):
        model = model or self.model()
        engine = IndividualModelingEngine(AgentDecisionEngine())
        engine.configure(model['individual_model']['agent_types'], execution_contract='module-assembly-v1')
        return engine

    def test_compiled_contract_preserves_parameters_and_rejects_unknown_or_null(self):
        requirement=requirement_payload()
        modeling=configured_modeling(requirement)
        modeling['individual']['execution_contract']='module-assembly-v1'
        agent=modeling['individual']['agent_types'][0]
        agent['algorithm_parameters']={'emotion':{'inertia':.2},'game':{'rationality':5}}
        confirm_modeling(modeling)
        model=compile_artificial_society({'requirement':requirement,'requirement_id':'req','fingerprint':'sha256:test'},modeling)
        self.assertEqual(agent['algorithm_parameters'],model['individual_model']['agent_types'][0]['algorithm_parameters'])
        self.assertEqual('module-assembly-v1',model['runtime_binding']['implementation_version'])
        self.assertEqual(model,ArtificialSocietySpec.parse(model).payload)
        for version in [None,'unknown']:
            bad=deepcopy(modeling)
            bad['individual']['execution_contract']=version
            confirm_modeling(bad)
            with self.assertRaises(ValueError):
                compile_artificial_society({'requirement':requirement,'requirement_id':'req','fingerprint':'sha256:test'},bad)

    def test_reconfigure_resets_contexts_and_counters_without_sharing_runs(self):
        engine=self.configure()
        profile,state,observation=context(role='leisure_visitor')
        engine.rule_plan(profile,state,observation)
        self.assertTrue(engine._boundary_contexts)
        old=engine.assemblies
        engine.configure(self.model()['individual_model']['agent_types'],execution_contract='module-assembly-v1')
        self.assertEqual({},engine._boundary_contexts)
        self.assertEqual({},engine.emotional_states)
        self.assertEqual(0,engine.pipeline_decisions)
        self.assertIsNot(old['leisure_visitor'].emotion,engine.assemblies['leisure_visitor'].emotion)
        other=self.configure()
        engine.rule_plan(profile,state,observation)
        self.assertEqual({},other.emotional_states)
        engine.retain_active_people([])
        self.assertEqual({},engine._boundary_contexts)
        self.assertEqual({},engine.emotional_states)
        self.assertTrue(engine.diagnostics()['pipeline_enabled'])
        self.assertFalse(engine.diagnostics()['llm_enabled'])

    def test_same_boundary_freezes_state_and_malformed_parameters_reject(self):
        engine=self.configure()
        profile,state,observation=context(role='consumer')
        first=engine.rule_plan(profile,state,observation)
        state.fatigue=1
        state.perceived_risk=1
        self.assertEqual(first,engine.rule_plan(profile,state,observation))
        for parameters in [None,False,[],{'game':{'rationality':True}}]:
            agents=deepcopy(self.model()['individual_model']['agent_types'])
            next(a for a in agents if a['id']=='consumer')['algorithm_parameters']=parameters
            with self.assertRaises(ValueError):
                engine.configure(agents,execution_contract='module-assembly-v1')
        agents=deepcopy(self.model()['individual_model']['agent_types'])
        agents[0]['algorithm_parameters']={}
        with self.assertRaisesRegex(ValueError,'require'):
            engine.configure(agents)

    def test_opt_in_assembles_only_selected_modules(self):
        engine = self.configure()
        profile, state, observation = context(role='consumer')
        plan = engine.rule_plan(profile, state, observation)
        self.assertEqual('bounded_rational', plan.source)
        assembly = engine.diagnostics()['module_assembly']
        self.assertEqual(['perception', 'memory', 'goal', 'game', 'decision', 'action', 'learning'],
                         assembly['roles']['consumer']['order'])
        self.assertEqual('awaiting_learning_policy', assembly['roles']['consumer']['learning_status'])
        self.assertEqual(0, len(engine.replay))

    def test_disabled_emotion_participation_is_isolated_from_game(self):
        model = self.model()
        consumer = next(a for a in model['individual_model']['agent_types'] if a['id']=='consumer')
        consumer['capabilities']['emotion'] = True
        consumer['emotionState'] = {'method':'PAD 连续情感模型','participates_in_operation':False}
        engine = self.configure(model)
        profile, state, observation = context(role='consumer')
        game = engine.assemblies['consumer'].game
        game.choose = Mock(wraps=game.choose)
        engine.rule_plan(profile, state, observation)
        emotion_input = game.choose.call_args.args[3]
        self.assertEqual(0, emotion_input.pleasure)
        self.assertEqual(0, emotion_input.dominance)
        self.assertIn(profile.person_id, engine.emotional_states)

    def test_same_snapshot_never_updates_emotion_twice(self):
        engine = self.configure()
        profile, state, observation = context(role='leisure_visitor')
        pad = engine.assemblies['leisure_visitor'].emotion
        pad.update = Mock(wraps=pad.update)
        first = engine.rule_plan(profile, state, observation)
        second = engine.rule_plan(profile, state, observation)
        self.assertEqual(first, second)
        self.assertEqual(1, pad.update.call_count)
        engine.rule_plan(profile, state, replace(observation,snapshot_id='next',time_seconds=11))
        self.assertEqual(2, pad.update.call_count)

    def test_role_parameters_and_individual_states_do_not_leak(self):
        model=self.model()
        leisure=next(a for a in model['individual_model']['agent_types'] if a['id']=='leisure_visitor')
        leisure['algorithm_parameters']={'emotion':{'inertia':0.1}}
        engine=self.configure(model)
        self.assertEqual(.1,engine.assemblies['leisure_visitor'].emotion.inertia)
        self.assertEqual(.65,engine.assemblies['social_visitor'].emotion.inertia)
        self.assertIsNot(engine.assemblies['leisure_visitor'].emotion,engine.assemblies['social_visitor'].emotion)
        profile,state,observation=context(role='leisure_visitor')
        engine.rule_plan(profile,state,observation)
        profile2=replace(profile,person_id='p2')
        state2=replace(state,person_id='p2')
        observation2=replace(observation,person_id='p2',snapshot_id='s2')
        engine.rule_plan(profile2,state2,observation2)
        self.assertEqual({'p1','p2'},set(engine.emotional_states))
        leisure['algorithm_parameters']['emotion']['inertia']=.9
        self.assertEqual(.1,engine.assemblies['leisure_visitor'].emotion.inertia)

    def test_unregistered_methods_and_invalid_parameters_fail_atomically(self):
        engine=self.configure()
        old=engine.assemblies
        for change in ['method','parameter','version']:
            model=self.model()
            agents=model['individual_model']['agent_types']
            if change=='method':agents[0]['architecture'][0]['method']='unimplemented'
            if change=='parameter':agents[0]['algorithm_parameters']={'emotion':{'inertia':float('nan')}}
            with self.assertRaises(ValueError):
                engine.configure(agents,execution_contract='bad' if change=='version' else 'module-assembly-v1')
            self.assertIs(old,engine.assemblies)

    def test_llm_keeps_run_provider_and_neutral_nonparticipating_emotion(self):
        model=self.model()
        agent=next(a for a in model['individual_model']['agent_types'] if a['id']=='consumer')
        agent['capabilities']['emotion']=True
        agent['emotionState']={'method':'PAD 连续情感模型','participates_in_operation':False}
        next(m for m in agent['architecture'] if m['id']=='decision')['method']='LLM-driven'
        engine=self.configure(model)
        engine.llm.base_url='http://example.invalid/v1'
        engine.llm.model='qa-provider'
        engine.llm.api_key='qa-placeholder'  # QA stand-in; external mode requires a configured key
        decision=engine.assemblies['consumer'].decision
        async def respond(_messages):
            self.assertEqual('qa-provider',decision.model)
            self.assertEqual('http://example.invalid/v1',decision.base_url)
            return {'action':'continue','target_id':None,'reason':'test','confidence':.9}
        decision._request=AsyncMock(side_effect=respond)
        profile,state,observation=context(role='consumer')
        plan=asyncio.run(engine.decide(profile,state,observation))
        self.assertEqual('llm_driven',plan.source)
        context_json=__import__('json').loads(decision._request.call_args.args[0][1]['content'])
        self.assertEqual(0,context_json['pad_emotion']['dominance'])
        self.assertEqual(1,engine.diagnostics()['llm_decisions'])
        self.assertEqual(0,len(engine.replay))

    def test_legacy_configuration_retains_original_replay_semantics(self):
        engine=IndividualModelingEngine(AgentDecisionEngine())
        model=compiled_model()
        engine.configure(model['individual_model']['agent_types'])
        profile,state,observation=context(role='consumer')
        engine.rule_plan(profile,state,observation)
        self.assertEqual(1,len(engine.replay))
        self.assertEqual({},engine.assemblies)


if __name__=='__main__':unittest.main()
