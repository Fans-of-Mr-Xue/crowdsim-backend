"""Merge regressions: no real models, processes, network or SUMO required."""
import asyncio
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
import unittest
from unittest.mock import AsyncMock, Mock, patch

import start_backend
from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.domain.crowd_visual_state import CrowdVisualPolicy
from crowdsim.infrastructure.performance_probe import PerformanceOptions
from crowdsim_overlay_server import parse_args
from tests.test_modeling_algorithms import context, implemented_architecture
from tests.test_requirement_location_v2 import located_requirement


class StartupCompatibilityTests(unittest.TestCase):
    def test_legacy_server_without_monitor_endpoint_is_only_reused_without_monitoring(self):
        for options, accepted in (([], True), (['--monitor-performance'], False)):
            with self.subTest(options=options), patch('start_backend.get_json', side_effect=HTTPError(
                'http://localhost/api/v1/performance/config', 404, 'not found', None, None,
            )):
                if accepted:
                    start_backend.check_performance_reuse(parse_args(options))
                else:
                    with self.assertRaises(start_backend.StartupError):
                        start_backend.check_performance_reuse(parse_args(options))

    def test_ssh_defaults_and_existing_environment_are_compatible(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual('hyq', start_backend.tunnel_settings('DB')[0]['USER'])
        with patch.dict('os.environ', {'CROWDSIM_DB_SSH_USER': 'local-user'}):
            self.assertEqual('local-user', start_backend.tunnel_settings('DB')[0]['USER'])

    def test_occupied_optional_control_port_does_not_stop_required_services(self):
        supervisor = start_backend.Supervisor()
        core = Mock(pid=101)
        core.poll.return_value = None
        supervisor.children = [('SUMO', core, True)]
        service = start_backend.Service('control', 8766, ['python'], lambda: False, optional=True)
        with patch('start_backend.listening', return_value=True), patch('start_backend.os.killpg') as kill:
            self.assertFalse(supervisor.start_service(service))
            supervisor.check_children()
        self.assertEqual([('SUMO', core, True)], supervisor.children)
        kill.assert_not_called()

    def test_failed_optional_child_cleans_up_only_its_own_process_group(self):
        supervisor = start_backend.Supervisor()
        core, control = Mock(pid=101), Mock(pid=202)
        core.poll.return_value = None
        supervisor.children = [('SUMO', core, True)]
        service = start_backend.Service('control', 8766, ['python'], lambda: False, optional=True)

        def spawn(*_):
            supervisor.children.append(('control', control, True))

        with patch('start_backend.listening', return_value=False), patch.object(supervisor, 'spawn', side_effect=spawn), patch.object(
            supervisor, 'wait_ready', side_effect=start_backend.StartupError('not ready'),
        ), patch('start_backend.os.killpg') as kill:
            self.assertFalse(supervisor.start_service(service))
        self.assertEqual([('SUMO', core, True)], supervisor.children)
        self.assertEqual([202], [call.args[0] for call in kill.call_args_list])

    def test_optional_child_exit_after_startup_keeps_required_services_alive(self):
        supervisor = start_backend.Supervisor()
        core, control = Mock(pid=101), Mock(pid=202)
        core.poll.return_value = None
        control.poll.return_value = 1
        supervisor.children = [('SUMO', core, True), ('control', control, True)]
        supervisor.optional_names.add('control')
        with patch('start_backend.os.killpg') as kill:
            supervisor.check_children()
            supervisor.check_children()
        self.assertEqual([('SUMO', core, True)], supervisor.children)
        self.assertEqual([202], [call.args[0] for call in kill.call_args_list])

    def test_required_service_failure_is_still_fatal(self):
        supervisor = start_backend.Supervisor()
        service = start_backend.Service('SUMO', 8765, ['python'], lambda: False)
        with patch('start_backend.listening', return_value=True), self.assertRaises(start_backend.StartupError):
            supervisor.start_service(service)


class RuntimeCompatibilityTests(unittest.TestCase):
    def runtime(self, *, use_llm=False):
        root = Path(__file__).resolve().parents[1]
        requirement = located_requirement(True)
        requirement.update(research={'problem_type': 1}, experiment={'duration_seconds': 600, 'repetitions': 5})
        record = {'requirement_id': 'req-test', 'requirement': requirement}
        with patch('crowdsim.infrastructure.sumo_adapter.discover_sumo_binary', return_value='unused'):
            return SimulationRuntime(
                root / 'scenarios/shanghai_bund/bund.hotspot.sumocfg',
                use_llm=use_llm, requirement_record=record, random_seed=37,
                timeline_end_seconds=1800, performance_options=PerformanceOptions(True, False, 10),
            )

    def model(self, runtime):
        return {
            'model_id': 'asm-test', 'requirement_id': runtime.requirement_id,
            'artificial_society': {
                'individual_model': {'profile_parameters': {'risk_tolerance': {'min': .9, 'max': .9}},
                                     'agent_types': implemented_architecture()},
                'rule_model': {'runtime_parameters': {'decision_period_seconds': 9,
                              'visual_state': {'confirmation_duration_seconds': 12}}},
            },
        }

    def test_unbound_engine_preserves_rule_and_existing_llm_delegate(self):
        runtime = self.runtime(use_llm=True)
        self.addCleanup(runtime.close)
        profile, state, observation = context()
        fallback = runtime.decision_engine.fallback_engine
        self.assertEqual(AgentDecisionEngine().rule_plan(profile, state, observation),
                         runtime.decision_engine.rule_plan(profile, state, observation))
        fallback.decide = AsyncMock(return_value='original-gateway-result')
        self.assertEqual('original-gateway-result', asyncio.run(
            runtime.decision_engine.decide(profile, state, observation),
        ))
        fallback.decide.assert_awaited_once()

    def test_reset_keeps_bound_model_duration_seed_and_performance_options(self):
        runtime = self.runtime()
        self.addCleanup(runtime.close)
        model = self.model(runtime)
        runtime.configure_artificial_society(model)
        self.assertTrue(runtime.use_llm)
        with patch.object(SimulationRuntime, 'initialize'):
            runtime.reset(seed=91)
        self.assertEqual(model, runtime.artificial_society_record)
        self.assertEqual(600, runtime.timeline_end_seconds)
        self.assertEqual(1800, runtime._scenario_timeline_end_seconds)
        self.assertEqual(91, runtime.random_seed)
        self.assertEqual(PerformanceOptions(True, False, 10), runtime.performance_options)
        self.assertTrue(runtime.use_llm)

    def test_leaving_model_restores_all_launch_defaults_and_retains_requirement(self):
        for original_llm in (False, True):
            with self.subTest(original_llm=original_llm):
                runtime = self.runtime(use_llm=original_llm)
                self.addCleanup(runtime.close)
                base_config = deepcopy(runtime.population.profile_sampler.config)
                runtime.configure_artificial_society(self.model(runtime))
                runtime.close()
                runtime.clear_artificial_society()
                with patch.object(SimulationRuntime, 'initialize'):
                    runtime.reset(seed=91)
                self.assertIsNone(runtime.artificial_society_record)
                self.assertEqual(original_llm, runtime.use_llm)
                self.assertEqual(2, runtime.decision_scheduler.period_seconds)
                self.assertEqual(CrowdVisualPolicy.load(), runtime.visual_policy)
                self.assertEqual(base_config, runtime.population.profile_sampler.config)
                self.assertEqual({}, runtime.population.profile_sampler.agent_type_attributes)
                self.assertEqual('req-test', runtime.requirement_id)
                self.assertEqual(600, runtime.timeline_end_seconds)

    def test_replacing_model_does_not_inherit_unspecified_parameters(self):
        runtime = self.runtime()
        self.addCleanup(runtime.close)
        runtime.configure_artificial_society(self.model(runtime))
        replacement = {'model_id': 'asm-other', 'requirement_id': runtime.requirement_id,
                       'artificial_society': {'individual_model': {}, 'rule_model': {}}}
        runtime.configure_artificial_society(replacement)
        self.assertFalse(runtime.use_llm)
        self.assertEqual(2, runtime.decision_scheduler.period_seconds)
        self.assertEqual(CrowdVisualPolicy.load(), runtime.visual_policy)

    def test_running_binding_cannot_be_removed(self):
        runtime = self.runtime()
        self.addCleanup(runtime.close)
        runtime.configure_artificial_society(self.model(runtime))
        runtime.state = RuntimeState.RUNNING
        with self.assertRaises(ValueError):
            runtime.clear_artificial_society()
        self.assertEqual('asm-test', runtime.artificial_society_id)
