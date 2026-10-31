"""Regression tests use the real native movement engines, not mock curves."""
import builtins
import copy
import json
import unittest
from unittest.mock import patch

from shapely.geometry import Point, shape

from data_service.prediction.catalog import method_catalog
from data_service.prediction.demo import demo_inputs, monument_inputs
from data_service.prediction.geometry import build_network
from data_service.prediction.inputs import normalize_methods, normalize_settings
from data_service.prediction.solver import compute_comparison
from data_service.prediction.runtime_budget import MAX_PEOPLE

METHODS = ['conservation', 'collision_free_speed', 'social_force']


class PredictionModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = demo_inputs()
        cls.result = compute_comparison(*cls.inputs, METHODS)

    def test_real_engines_share_geometry_time_and_conserve_people(self):
        self.assertEqual(self.result['method_errors'], [])
        self.assertEqual([r['method']['id'] for r in self.result['results']], METHODS)
        for result in self.result['results']:
            self.assertEqual([row['minute'] for row in result['series']], [0, 1, 2, 3])
            self.assertLess(result['summary']['mass_balance_error'], .00001)
            for row in result['series']:
                self.assertAlmostEqual(60 + row['cumulative_arrivals'], row['crowd'] + row['outside_queue'] + row['departed'], places=1)
                self.assertEqual(len(row['cells']['crowd']), len(self.result['network']['cells']))
                self.assertTrue(all(value >= 0 for value in row['cells']['crowd']))
                self.assertAlmostEqual(sum(row['cells']['crowd']), row['crowd'], delta=.1)
                self.assertGreaterEqual(row['outflow_p_min'], 0)
            if result['method']['id'] != 'conservation':
                self.assertEqual(result['diagnostics']['engine'], 'JuPedSim')
                self.assertEqual(result['diagnostics']['engine_version'], '1.4.2')
                self.assertIn('ModelState', result['diagnostics']['operational_model'])
        json.dumps(self.result, allow_nan=False)

    def test_obstacle_is_not_walkable_and_positions_are_real(self):
        obstacle = shape(self.inputs[1]['obstacles']['features'][0]['geometry'])
        for result in self.result['results'][1:]:
            for row in result['series']:
                self.assertEqual(len(row['agents']), row['crowd'])
                for person in row['agents']:
                    self.assertFalse(obstacle.contains(Point(person['position'])))
        cfs, sfm = self.result['results'][1:]
        self.assertEqual(cfs['series'][0]['agents'], sfm['series'][0]['agents'])
        self.assertNotEqual(cfs['series'][1]['agents'], sfm['series'][1]['agents'])

    def test_seed_reproduces_each_native_model(self):
        repeated = compute_comparison(*self.inputs, METHODS[1:])
        for first, second in zip(self.result['results'][1:], repeated['results']):
            self.assertEqual(first['series'], second['series'])

    def test_failed_second_repetition_does_not_publish_a_partial_batch(self):
        task, roads, settings = copy.deepcopy(self.inputs)
        settings['repetitions'] = 2
        first = copy.deepcopy(self.result['results'][0])
        with patch('data_service.prediction.solver.NetworkSolver.run', side_effect=[first, RuntimeError('second run failed')]):
            with self.assertRaisesRegex(ValueError, '第 2/2 次未完成'):
                compute_comparison(task, roads, settings, ['conservation'])

    def test_closed_gate_retains_demand_then_releases(self):
        task, roads, settings = copy.deepcopy(self.inputs)
        task['time_window'] = '2026-10-21 18:00 至 2026-10-21 18:02'
        settings.update(initial_crowd=0, entrance_shares=[0, 0, 1], arrival_rate_per_minute=12,
                        arrival_schedule=[{'minute': 0, 'rate': 12}, {'minute': 1, 'rate': 0}], emergency_open_minute=1)
        result = compute_comparison(task, roads, settings, METHODS)
        self.assertEqual(result['method_errors'], [])
        for model in result['results']:
            self.assertEqual(model['series'][1]['outside_queue'], 12)
            self.assertEqual(model['series'][1]['crowd'], 0)
            self.assertEqual(model['series'][2]['outside_queue'], 0)
            self.assertEqual(model['series'][2]['cumulative_arrivals'], 12)

    def test_oversized_micro_run_reports_partial_results_and_all_failed_error(self):
        task, roads, settings = copy.deepcopy(self.inputs)
        settings['initial_crowd'] = MAX_PEOPLE + 1
        result = compute_comparison(task, roads, settings, METHODS)
        self.assertEqual([r['method']['id'] for r in result['results']], ['conservation'])
        self.assertEqual(len(result['method_errors']), 2)
        self.assertEqual(result['selected_methods'], METHODS)
        with self.assertRaisesRegex(ValueError, str(MAX_PEOPLE)):
            compute_comparison(task, roads, settings, METHODS[1:])

    def test_full_window_survives_turnover_beyond_active_population_budget(self):
        task, roads, settings = copy.deepcopy(self.inputs)
        task['time_window'] = '2026-10-21 18:00 至 2026-10-21 19:15'
        settings.update(initial_crowd=5, arrival_rate_per_minute=2, arrival_schedule=[], sample_minutes=15)
        # Cumulative 155 exceeds this active-population cap, but people leave.
        with patch('data_service.prediction.microscopic.MAX_PEOPLE', 30):
            result = compute_comparison(task, roads, settings, METHODS[1:])
        self.assertEqual(result['method_errors'], [])
        for model in result['results']:
            self.assertEqual([row['minute'] for row in model['series']], [0, 15, 30, 45, 60, 75])
            self.assertEqual(model['series'][-1]['cumulative_arrivals'], 150)
            self.assertTrue(model['diagnostics']['complete_time_window'])
            for row in model['series']:
                self.assertEqual(len(row['agents']), row['crowd'])
                self.assertEqual(5 + row['cumulative_arrivals'], row['crowd'] + row['departed'] + row['outside_queue'])

    def test_runtime_budget_failure_does_not_publish_truncated_micro_trajectories(self):
        with patch('data_service.prediction.microscopic.MAX_AGENT_STEPS', 50):
            result = compute_comparison(*self.inputs, METHODS)
        self.assertEqual([row['method']['id'] for row in result['results']], ['conservation'])
        self.assertEqual(len(result['method_errors']), 2)
        self.assertTrue(all('完整时间窗未完成' in row['message'] for row in result['method_errors']))

    def test_missing_engine_never_returns_disguised_macro_result(self):
        original = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == 'jupedsim':
                raise ImportError('test missing engine')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=importing):
            result = compute_comparison(*self.inputs, METHODS)
        self.assertEqual([r['method']['id'] for r in result['results']], ['conservation'])
        self.assertTrue(all('尚未安装' in e['message'] for e in result['method_errors']))

    def test_new_settings_validate_and_default_selection_contains_three(self):
        task, _, _ = self.inputs
        self.assertEqual([m['id'] for m in method_catalog() if m.get('recommended')], METHODS)
        self.assertEqual(normalize_methods(METHODS), METHODS)
        for key, bad in [('agent_radius_m', 0), ('cfs_time_gap_s', -1), ('sfm_reaction_time_s', float('nan'))]:
            with self.assertRaises(ValueError):
                normalize_settings(task, {key: bad})

    def test_monument_uses_existing_network_and_only_closes_precision_gaps(self):
        task, roads, settings = monument_inputs()
        network = build_network(task, roads, settings)
        self.assertFalse(settings['allow_boundary_walkable'])
        self.assertEqual(roads['source']['filename'], 'bund.net.xml')
        self.assertEqual(roads['source']['scenario_id'], 'people_heroes_monument')
        # Millimetre gaps previously removed the entire monument ring from the
        # selected component. Metre-scale disconnected pieces must remain out.
        self.assertGreater(network['public']['walkable_area_m2'], 1600)
        self.assertGreater(network['public']['discarded_walkable_area_m2'], 40)
        self.assertGreater(network['public']['component_count'], 1)
        self.assertEqual(network['public']['sumo_join_tolerance_m'], .01)
        self.assertTrue(all(gate['snap_distance_m'] < .01 for gate in network['public']['gates']))
        self.assertEqual(network['public']['core_position_mode'], 'configured')

    def test_monument_all_three_engines_complete(self):
        result = compute_comparison(*monument_inputs(), METHODS)
        self.assertEqual(result['method_errors'], [])
        self.assertEqual(len(result['results']), 3)
        for model in result['results']:
            tail = model['series'][-1]
            self.assertAlmostEqual(tail['crowd'] + tail['outside_queue'] + tail['departed'], 96, places=1)
            self.assertLess(model['summary']['mass_balance_error'], 1e-5)

    def test_force_model_stays_inside_narrow_monument_paths_with_continuous_arrivals(self):
        task, roads, settings = monument_inputs()
        settings.update(initial_crowd=100, arrival_rate_per_minute=87.04, arrival_schedule=[],
                        dwell_minutes=30, grid_m=12, path_width_m=4, free_speed_mps=1.15,
                        attraction_point=None, exit_points={}, exit_labels={}, exit_points_note='',
                        entrance_shares=[.7, .3, 0], arrival_interpolation='smooth')
        result = compute_comparison(task, roads, settings, ['social_force'])
        self.assertEqual(result['method_errors'], [])
        model = result['results'][0]
        self.assertEqual(model['diagnostics']['step_seconds'], .01)
        self.assertEqual(model['series'][-1]['minute'], 5)
        self.assertEqual(model['series'][-1]['cumulative_arrivals'], 435)
        network = build_network(task, roads, settings)
        from shapely.ops import transform
        geometry = transform(network['to_wgs84'], network['walkable_geometry']).buffer(1e-10)
        for row in model['series']:
            self.assertEqual(len(row['agents']), row['crowd'])
            self.assertTrue(all(geometry.covers(Point(agent['position'])) for agent in row['agents']))


if __name__ == '__main__':
    unittest.main()
