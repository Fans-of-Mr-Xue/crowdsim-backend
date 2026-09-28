import asyncio
import csv
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crowdsim.domain.crowd_visual_state import CrowdVisualPolicy, STATE_COLORS
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot, Observation
from crowdsim.core.state_updater import StateUpdater


def sample(density=0.1, speed=1.0, now=10.0):
    motion = MotionSnapshot('a', now, 0, 0, 0, 0, speed, 'e', 'e_0', 0, 0, 0, 2)
    return Observation('a', 'sample', now, motion, objective_density_per_m2=density)


class CrowdVisualStateTests(unittest.TestCase):
    def setUp(self):
        self.policy = CrowdVisualPolicy()
        self.updater = StateUpdater(self.policy)

    def advance(self, state, density, speed=0.0, seconds=5.0):
        for i in range(round(seconds / 0.5)):
            obs = sample(density, speed, 10 + (i + 1) * 0.5)
            state = self.updater.update_all({'a': state}, {'a': AgentProfile('a')}, {'a': obs}, 0.5)['a']
        return state

    def classify(self, state=None, density=0.1, speed=1.0):
        obs = sample(density, speed)
        return self.policy.classify(obs.own_motion, obs, state or AgentState('a'))

    def test_density_boundaries_and_priority(self):
        for density, name in [(0.49, 'normal'), (0.5, 'busy'), (1.49, 'busy'), (1.5, 'crowded'), (4, 'crowded')]:
            with self.subTest(density=density):
                result = self.classify(density=density)
                self.assertEqual(name, result['visual_state'])
                self.assertEqual(STATE_COLORS[name], result['color'])
        stop = AgentState('a', activity_state='hotspot_dwelling')
        self.assertEqual('planned_stop', self.classify(stop, density=0.8)['visual_state'])
        self.assertEqual('crowded', self.classify(stop, density=1.5)['visual_state'])

    def test_critical_density_needs_five_simulation_seconds(self):
        state = self.advance(AgentState('a'), 4, speed=1, seconds=4.5)
        self.assertEqual('crowded', self.classify(state, 4)['visual_state'])
        state = self.advance(state, 4, speed=1, seconds=0.5)
        self.assertEqual('high_risk', self.classify(state, 4)['visual_state'])

    def test_joint_condition_does_not_borrow_previous_low_speed_time(self):
        state = self.advance(AgentState('a'), 0.1, seconds=10)
        state = self.advance(state, 3.5, seconds=0.5)
        self.assertEqual(10.5, state.low_speed_duration)
        self.assertEqual(0.5, state.dense_low_speed_duration)
        self.assertEqual('crowded', self.classify(state, 3.5, 0)['visual_state'])
        state = self.advance(state, 3.5, seconds=4.5)
        self.assertEqual('high_risk', self.classify(state, 3.5, 0)['visual_state'])

    def test_planned_stop_can_be_high_risk_without_being_behaviorally_blocked(self):
        state = self.advance(AgentState('a', activity_state='hotspot_dwelling'), 3.5)
        self.assertEqual(0, state.blocked_duration)
        self.assertEqual(0, state.visual_blocked_duration)
        self.assertEqual('high_risk', self.classify(state, 3.5, 0)['visual_state'])

    def test_unplanned_low_speed_is_yellow_only_after_confirmation(self):
        state = self.advance(AgentState('a'), 0.1, seconds=4.5)
        self.assertEqual('normal', self.classify(state, 0.1, 0)['visual_state'])
        state = self.advance(state, 0.1, seconds=0.5)
        self.assertEqual('busy', self.classify(state, 0.1, 0)['visual_state'])
        state = self.advance(state, 0.1, speed=0.2, seconds=0.5)
        self.assertEqual(0, state.low_speed_duration)
        self.assertEqual('normal', self.classify(state, 0.1, 0.2)['visual_state'])

    def test_unknown_density_interrupts_confirmation_and_is_explicit(self):
        state = self.advance(AgentState('a'), 4, seconds=4.5)
        state = self.advance(state, None, seconds=0.5)
        self.assertEqual(0, state.critical_density_duration)
        self.assertEqual(0, state.dense_low_speed_duration)
        result = self.classify(density=None)
        self.assertIsNone(result['density_person_per_m2'])
        self.assertFalse(result['density_valid'])
        self.assertEqual('density_unavailable', result['visual_reason'])

    def test_dropping_density_resets_both_high_risk_timers(self):
        state = self.advance(AgentState('a'), 4)
        state = self.advance(state, 3.49, seconds=0.5)
        self.assertEqual(0, state.critical_density_duration)
        self.assertEqual(0, state.dense_low_speed_duration)

    def test_expired_planned_wait_does_not_stay_blue(self):
        self.assertEqual('planned_stop', self.classify(AgentState('a', planned_wait_until=11))['visual_state'])
        self.assertEqual('normal', self.classify(AgentState('a', planned_wait_until=10))['visual_state'])

    def test_custom_display_policy_does_not_change_behavior_blocked_timer(self):
        updater = StateUpdater(replace(self.policy, low_speed_threshold_mps=0.4))
        obs = sample(0.1, 0.3)
        state = updater.update_all({'a': AgentState('a')}, {'a': AgentProfile('a')}, {'a': obs}, 0.5)['a']
        self.assertEqual(0, state.blocked_duration)
        self.assertEqual(0.5, state.visual_blocked_duration)

    def test_real_config_loaded_and_rejects_invalid_thresholds(self):
        self.assertEqual(self.policy, CrowdVisualPolicy.load())
        with self.assertRaises(ValueError):
            replace(self.policy, critical_density_person_per_m2=1)
        with self.assertRaises(ValueError):
            replace(self.policy, confirmation_duration_seconds=float('nan'))


class CrowdVisualRuntimeTests(unittest.TestCase):
    def test_sumo_frames_and_recorded_evidence_agree(self):
        from crowdsim.core.simulation_runtime import SimulationRuntime
        from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
        root = Path(__file__).resolve().parents[1]
        scenario = root / 'tests/scenarios/unidirectional_corridor'
        with TemporaryDirectory(prefix='crowd-visual-smoke-') as directory:
            def recorder(*args, **kwargs):
                return ExperimentRecorder(*args, **kwargs, root=Path(directory))
            with patch('crowdsim.core.simulation_runtime.ExperimentRecorder', side_effect=recorder), \
                 patch('crowdsim.core.simulation_runtime.PROJECT_ROOT', Path(directory)):
                runtime = SimulationRuntime(scenario / 'scenario.sumocfg', pedestrian_route_files=[scenario / 'demand.rou.xml'])
                try:
                    runtime.initialize()
                    self.assertEqual(1, runtime.init_frame()['crowd_visual_state']['version'])
                    runtime.start()
                    for _ in range(12):
                        asyncio.run(runtime.tick_async())
                    frame = runtime.frame()
                    self.assertTrue(frame['pedestrians'])
                    self.assertEqual(len(frame['pedestrians']), sum(frame['metrics']['visual_state_counts'].values()))
                    for person in frame['pedestrians']:
                        self.assertEqual(STATE_COLORS[person['visual_state']], person['color'])
                        self.assertIn('dense_low_speed_duration_seconds', person)
                    # Serializing repeatedly must not advance confirmation timers.
                    self.assertEqual(frame['pedestrians'], runtime.frame()['pedestrians'])
                    output = runtime.recorder.directory
                    with (output / 'trajectory.csv').open() as handle:
                        rows = list(csv.DictReader(handle))
                    self.assertIn('visual_reason', rows[0])
                    last = {row['person_id']: row for row in rows if float(row['time']) == runtime.time_seconds}
                    for person in frame['pedestrians']:
                        self.assertEqual(person['visual_state'], last[person['id']]['visual_state'])
                    manifest = json.loads((output / 'manifest.json').read_text())
                    self.assertEqual(runtime.visual_policy.metadata(), manifest['crowd_visual_state'])
                    runtime.reset()
                    self.assertFalse(runtime.visual_states)
                finally:
                    runtime.close()


if __name__ == '__main__':
    unittest.main()
