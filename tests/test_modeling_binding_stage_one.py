"""Stage-one binding contracts; no engine start, network or formal-model writes."""
from copy import deepcopy
import unittest

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.domain.artificial_society_spec import ArtificialSocietySpec
from crowdsim.modeling_algorithm.compiler import compile_artificial_society
from tests.test_modeling_requirements_compiler import requirement_payload, configured_modeling, confirm_modeling
from tests.test_backend_merge_contracts import RuntimeCompatibilityTests


def compiled_model():
    requirement = requirement_payload()
    requirement['population']['distributions']['crowd_role'] = [
        {'code': code, 'label': label, 'percent': 25}
        for code, label in [('leisure_visitor', '休闲游客'), ('social_visitor', '社交游客'),
                            ('consumer', '消费者'), ('commuter', '通勤者')]]
    modeling = configured_modeling(requirement)
    for agent in modeling['individual']['agent_types']:
        emotion = agent['id'] in {'leisure_visitor', 'social_visitor'}
        game = agent['id'] == 'consumer'
        learning = agent['id'] != 'commuter'
        agent['capabilities'] = {'emotion': emotion, 'game': game, 'learning': learning}
        agent['emotionState'] = {'method': 'PAD 连续情感模型' if emotion else '',
                                 'participates_in_operation': emotion}
        agent['architecture'] = [
            {'id': 'perception', 'method': '运行时局部观察'},
            {'id': 'memory', 'method': '运行时有限状态'},
            *([{'id': 'desire', 'method': '情感驱动欲望'}] if emotion else []),
            {'id': 'goal', 'method': '运行时目标与路线'},
            *([{'id': 'game', 'method': '有限理性博弈'}] if game else []),
            {'id': 'decision', 'method': '运行时规则决策'},
            {'id': 'action', 'method': 'SUMO striping'},
            *([{'id': 'learning', 'method': '经验回放'}] if learning else [])]
    confirm_modeling(modeling)
    return compile_artificial_society({'requirement': requirement, 'requirement_id': 'req-test',
                                      'fingerprint': 'sha256:test'}, modeling)


class StageOneBindingTests(unittest.TestCase):
    def test_compiler_keeps_capabilities_and_adds_versioned_binding(self):
        model = compiled_model()
        binding = model['runtime_binding']
        self.assertEqual('confirmed-configuration-v1', binding['version'])
        self.assertEqual('configuration_only', binding['semantics'])
        roles = {row['role_id']: row for row in binding['roles']}
        for role in ['leisure_visitor', 'social_visitor', 'consumer']:
            self.assertTrue(roles[role]['capabilities']['learning'])
        self.assertFalse(roles['commuter']['capabilities']['learning'])
        self.assertEqual('运行时规则决策', next(x['method'] for x in roles['consumer']['architecture'] if x['id']=='decision'))
        self.assertEqual(model, ArtificialSocietySpec.parse(model).payload)

    def test_tampered_summary_and_unknown_version_are_rejected(self):
        model = compiled_model()
        bad = deepcopy(model)
        role = next(row for row in bad['runtime_binding']['roles'] if row['role_id'] == 'consumer')
        role['capabilities']['learning'] = False
        with self.assertRaisesRegex(ValueError, 'runtime binding'):
            ArtificialSocietySpec.parse(bad)
        bad = deepcopy(model)
        bad['runtime_binding']['version'] = 'unsupported'
        with self.assertRaisesRegex(ValueError, 'runtime binding'):
            ArtificialSocietySpec.parse(bad)

    def test_legacy_model_keeps_original_payload_and_fingerprint(self):
        model = compiled_model()
        model.pop('runtime_binding')
        before = deepcopy(model)
        spec = ArtificialSocietySpec.parse(model)
        self.assertEqual(before, spec.payload)
        self.assertEqual(spec.fingerprint, ArtificialSocietySpec.parse(before).fingerprint)

    def test_failed_rebind_retains_engine_profile_and_model(self):
        helper = RuntimeCompatibilityTests()
        runtime = helper.runtime()
        self.addCleanup(runtime.close)
        old = helper.model(runtime)
        runtime.configure_artificial_society(old)
        sampler = deepcopy(runtime.population.profile_sampler.config)
        old_engine = runtime.decision_engine
        old_binding = deepcopy(runtime.environment_model_binding)
        old_period = runtime.decision_scheduler.period_seconds
        for kind in ['location', 'sumo_config', 'rules', 'profile']:
            with self.subTest(kind=kind):
                bad = deepcopy(old)
                bad['model_id'] = 'asm-bad'
                if kind == 'location':
                    bad['artificial_society']['environment_model']['scenario_binding']['location_id'] = 'wrong'
                elif kind == 'sumo_config':
                    bad['artificial_society']['environment_model']['spatial_structure']['sumo_config'] = 'wrong.sumocfg'
                elif kind == 'rules':
                    bad['artificial_society']['rule_model']['rules'] = []
                else:
                    bad['artificial_society']['individual_model']['profile_parameters'] = {'risk_tolerance': {'min': 1, 'max': 0}}
                with self.assertRaises(ValueError):
                    runtime.configure_artificial_society(bad)
                self.assertIs(old_engine, runtime.decision_engine)
                self.assertEqual(sampler, runtime.population.profile_sampler.config)
                self.assertEqual(old_binding, runtime.environment_model_binding)
                self.assertEqual(old_period, runtime.decision_scheduler.period_seconds)
                self.assertEqual(old, runtime.artificial_society_record)

    def test_versioned_binding_is_verified_and_exposed_before_engine_start(self):
        helper = RuntimeCompatibilityTests()
        runtime = helper.runtime()
        self.addCleanup(runtime.close)
        payload = compiled_model()
        spec = ArtificialSocietySpec.parse(payload)
        record = {'model_id': 'asm-stage-one', 'requirement_id': 'req-test',
                  'fingerprint': f'sha256:{spec.fingerprint}', 'artificial_society': payload}
        runtime.configure_artificial_society(record)
        self.assertEqual(payload['runtime_binding'], runtime.artificial_society_diagnostics()['runtime_binding'])
        self.assertTrue(runtime.decision_engine.role_configs['consumer'].replay_enabled)
        self.assertFalse(runtime.use_llm)
        old_engine = runtime.decision_engine
        bad = deepcopy(record)
        bad['fingerprint'] = 'sha256:wrong'
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            runtime.configure_artificial_society(bad)
        self.assertIs(old_engine, runtime.decision_engine)
        runtime.clear_artificial_society()
        self.assertIsNone(runtime.artificial_society_diagnostics())


if __name__ == '__main__':
    unittest.main()
