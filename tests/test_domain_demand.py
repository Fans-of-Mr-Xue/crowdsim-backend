"""Multisource estimation: units, exact arrivals, source replacement and revision safety."""
import copy
import json
import unittest
from pathlib import Path

import mongomock
from mongomock.gridfs import enable_gridfs_integration

from data_service.prediction.arrivals import ArrivalProfile
from data_service.prediction.inputs import default_settings
from data_service.repositories.datasets import DatasetConflictError
from data_service.services.domain_portrait import DomainPortraitService, build_portrait
from data_service.services import domain_demand as demand
from data_service.services.domain_relations import occupancy_estimate


class DomainDemandTests(unittest.TestCase):
    def setUp(self):
        enable_gridfs_integration()
        self.service = DomainPortraitService(mongomock.MongoClient().db)
        roads = json.loads((Path(__file__).resolve().parents[1] / 'data_service/resources/geography/shanghai-monument-sumo.geojson').read_text())
        self.task = self.service.tasks.save(dict(title='多源估计验证', time_window='2026-12-31 20:00 至 2027-01-01 00:30',
            region={'mode': 'custom', 'name': '纪念塔', 'geometry': roads['boundary']}, components={'forecast_setup': {'initial_crowd': 150, 'dwell_minutes': 5, 'seed': 9}}))
        self.config = demand.initialize(self.task, build_portrait(self.task))
        # Legacy saved tasks used feeder counts; their previous results remain stable.
        self.config.pop('transport_use')

    def save(self, section, data, task=None):
        task = task or self.task
        return self.service.save(task['id'], dict(revision=task['revision'], section=section, data=data))

    def test_full_window_count_matches_actual_solver_arrival_integral(self):
        result = demand.estimate(self.task, self.config)
        self.assertEqual(len(result['bins']), 9)
        self.assertEqual(result['historical_mean'], 557000)
        expected = round(557000 * .015 * .35 * sum(r['passages'] for r in self.config['transport']['records']) / sum(r['reference_passages'] for r in self.config['transport']['records']) * .9)
        self.assertEqual(result['total_visits'], expected)
        self.assertAlmostEqual(ArrivalProfile(result['forecast_inputs'], 270).cumulative(270), expected)
        self.assertEqual(result['bins'][-1]['end'], '2027-01-01T00:30:00')

    def test_neutral_factors_and_unequal_weather_apply_per_interval_not_twice(self):
        config = copy.deepcopy(self.config)
        for row, weather in zip(config['transport']['records'], config['weather']['records']):
            row['passages'] = row['reference_passages']
            weather['factor'] = 1
        result = demand.estimate(self.task, config)
        self.assertEqual(result['total_visits'], round(result['base_visits']))
        config['weather']['records'][0]['factor'] = 0
        reduced = demand.estimate(self.task, config)
        self.assertEqual(reduced['bins'][0]['visits'], 0)
        share = config['transport']['records'][0]['passages'] / sum(r['passages'] for r in config['transport']['records'])
        self.assertEqual(reduced['total_visits'], round(result['base_visits'] * (1 - share)))

    def test_zero_traffic_and_short_final_bin(self):
        task = {**self.task, 'time_window': '2026-12-31 20:00 至 2026-12-31 21:05'}
        config = demand.initialize(task, build_portrait(task))
        config['transport_use'] = 'counts'
        result = demand.estimate(task, config)
        self.assertEqual(result['bins'][-1]['minutes'], 5)
        self.assertAlmostEqual(ArrivalProfile(result['forecast_inputs'], 65).cumulative(65), result['total_visits'])
        for row in config['transport']['records']:
            row['passages'] = 0
        result = demand.estimate(task, config)
        self.assertEqual(result['total_visits'], 0)
        self.assertEqual(ArrivalProfile(result['forecast_inputs'], 65).cumulative(65), 0)

    def test_current_road_context_does_not_use_legacy_mock_counts(self):
        config = demand.initialize(self.task, build_portrait(self.task))
        self.assertEqual(config['transport_use'], 'context')
        result = demand.estimate(self.task, config)
        for row in config['transport']['records']:
            row['passages'] = 0
            row['reference_passages'] = 12345
        changed = demand.estimate(self.task, config)
        self.assertEqual(changed['forecast_inputs'], result['forecast_inputs'])
        self.assertEqual(result['total_visits'], 2632)
        self.assertEqual(result['transport_factor'], 1)
        self.assertEqual(len({round(b['rate'], 10) for b in result['bins']}), 1)
        self.assertAlmostEqual(ArrivalProfile(result['forecast_inputs'], 270).cumulative(270), 2632)

    def test_rejects_gaps_units_invalid_numbers_and_changed_scope(self):
        for change in [lambda c: c['transport']['records'].pop(),
                       lambda c: c['weather']['records'][0].update(factor=float('nan')),
                       lambda c: c['transport'].update(unit='vehicles/hour'),
                       lambda c: c['transport']['records'][1].update(start='2026-12-31T20:31:00'),
                       lambda c: c['transport']['records'][0].update(reference_passages=0),
                       lambda c: c['assumptions'].update(local_share=True),
                       lambda c: c['history']['records'].append(c['history']['records'][0]),
                       lambda c: c['scope'].update(geometry_hash='different')]:
            config = copy.deepcopy(self.config)
            change(config)
            with self.assertRaises(ValueError):
                demand.estimate(self.task, config)

    def test_apply_is_explicit_preserves_other_inputs_and_detects_edits(self):
        task = self.save('demand_initialize', {})
        self.assertEqual(default_settings(task), default_settings(self.task))
        portrait = build_portrait(task)
        self.assertFalse(portrait['demand']['applied'])
        task = self.save('demand_apply', {'fingerprint': portrait['demand']['fingerprint']}, task)
        self.assertTrue(build_portrait(task)['demand']['applied'])
        self.assertEqual(default_settings(task)['initial_crowd'], 150)
        self.assertEqual(default_settings(task)['dwell_minutes'], 5)
        self.assertEqual(default_settings(task)['seed'], 9)
        self.assertEqual(task['components']['domain_portrait']['demand_application']['task_revision'], task['revision'])
        # Source replacement recalculates, without silently rewriting experiments.
        dataset = copy.deepcopy(self.config['transport'])
        dataset.update(kind='observed', source='测试断面计数导入')
        dataset['records'][0]['passages'] *= 2
        edited = self.save('demand_source', {'source': 'transport', 'dataset': dataset}, task)
        self.assertFalse(build_portrait(edited)['demand']['applied'])
        self.assertEqual(default_settings(edited), default_settings(task))
        with self.assertRaises(DatasetConflictError):
            self.save('demand_apply', {'fingerprint': portrait['demand']['fingerprint']}, task)
        with self.assertRaises(ValueError):
            self.save('demand_apply', {'fingerprint': portrait['demand']['fingerprint']}, edited)
        # Even if sources are unchanged, an experiment's manual curve edit is detected.
        changed = copy.deepcopy(task)
        changed['components']['forecast_setup']['arrival_schedule'][0]['rate'] += 1
        self.assertFalse(build_portrait(changed)['demand']['applied'])

    def test_read_is_non_mutating_and_time_changes_make_sources_stale(self):
        task = self.save('demand_initialize', {})
        before = copy.deepcopy(task)
        build_portrait(task)
        self.assertEqual(task, before)
        task['time_window'] = '2026-12-31 19:00 至 2027-01-01 00:30'
        self.assertEqual(build_portrait(task)['demand']['status'], 'stale')

    def activity_config(self):
        config = demand.initialize(self.task, build_portrait(self.task))
        config['activities'] = [dict(id='test', source_id='activity_test', title='测试活动', source_label='人工测试假设', source_url='',
            start='2026-12-31T20:05', end='2026-12-31T20:15', attendance=200, visit_share=.5,
            additional_share=.4, enabled=True, note='测试：半数到访，六成已计入历史基准')]
        return config

    def test_boundary_change_preserves_read_only_preview_but_cannot_apply(self):
        task = self.save('demand_initialize', {})
        original = build_portrait(task)['demand']
        changed = copy.deepcopy(task)
        changed['region']['geometry']['coordinates'][0][0][0] += .0001
        before = copy.deepcopy(changed)
        stale = build_portrait(changed)['demand']
        preview = stale['previous_estimate']
        self.assertEqual(stale['status'], 'stale')
        self.assertEqual(preview['total_visits'], original['total_visits'])
        self.assertEqual(preview['bins'], original['bins'])
        self.assertEqual(preview['scope'], original['scope'])
        self.assertIsNotNone(preview['occupancy'])
        self.assertNotIn('forecast_inputs', preview)
        self.assertNotIn('fingerprint', preview)
        self.assertFalse(preview['applied'])
        self.assertEqual(changed, before)
        with self.assertRaises(ValueError):
            demand.estimate(changed, changed['components']['domain_portrait']['demand_inputs'])
        changed['components']['domain_portrait']['demand_inputs']['weather']['records'][0]['factor'] = -1
        self.assertNotIn('previous_estimate', build_portrait(changed)['demand'])

    def test_time_change_previews_saved_intervals_without_stretching(self):
        task = self.save('demand_initialize', {})
        original = build_portrait(task)['demand']
        task['time_window'] = '2026-12-31 19:00 至 2027-01-01 01:30'
        stale = build_portrait(task)['demand']
        self.assertEqual(stale['previous_estimate']['bins'], original['bins'])
        self.assertEqual(stale['previous_estimate']['scope']['start'], '2026-12-31T20:00:00')
        self.assertEqual(stale['current_scope']['start'], '2026-12-31T19:00:00')

    def test_activity_preserves_total_and_time_interval_without_double_counting(self):
        config = self.activity_config()
        result = demand.estimate(self.task, config)
        curve = ArrivalProfile(result['forecast_inputs'], 270)
        self.assertEqual(result['activity_delta'], 40)
        self.assertEqual(result['total_visits'], 2672)
        self.assertAlmostEqual(curve.cumulative(270), 2672)
        self.assertAlmostEqual(curve.cumulative(5), 2632/270*5)
        self.assertAlmostEqual(curve.cumulative(15), 2632/270*15+40)
        self.assertAlmostEqual(curve.cumulative(45), 2632/270*45+40)
        config['activities'][0]['enabled'] = False
        self.assertEqual(demand.estimate(self.task, config)['total_visits'], 2632)

    def test_activity_validation_rejects_duplicate_source_and_bad_assumptions(self):
        for change in [lambda c:c['activities'].append({**c['activities'][0], 'id':'other'}),
                       lambda c:c['activities'][0].update(start='2026-09-08T20:00'),
                       lambda c:c['activities'][0].update(end='2026-12-31T20:05'),
                       lambda c:c['activities'][0].update(visit_share=1.2),
                       lambda c:c['activities'][0].update(additional_share=float('nan')),
                       lambda c:c['activities'][0].update(attendance=True),
                       lambda c:c['activities'][0].update(source_url='javascript:alert(1)'),
                       lambda c:c['activities'][0].update(start='2026-12-31T20:05+08:00')]:
            config = self.activity_config(); change(config)
            with self.assertRaises(ValueError):
                demand.estimate(self.task, config)

    def test_activity_save_is_separate_from_experiment_application(self):
        task = self.save('demand', self.activity_config())
        portrait = build_portrait(task)
        self.assertEqual(default_settings(task), default_settings(self.task))
        self.assertEqual(portrait['demand']['activity_delta'], 40)
        manual = next(r for r in portrait['activities']['records'] if r['source_id']=='activity_test')
        self.assertTrue(manual['counted'])
        self.assertEqual(manual['source'], '人工测试假设')
        historical = next(r for r in portrait['activities']['records'] if r['id']=='walking_event')
        self.assertFalse(historical['counted'])
        self.assertFalse(historical['time_matches'])
        task = self.save('demand_apply', {'fingerprint':portrait['demand']['fingerprint']}, task)
        self.assertAlmostEqual(ArrivalProfile(default_settings(task), 270).cumulative(270), 2672)

    def test_occupancy_accounts_for_stay_and_initial_departures(self):
        result = dict(status='ready', bins=[{'minute':0}], forecast_inputs={'arrival_rate_per_minute':2})
        estimate = occupancy_estimate(self.task, result, {'dwell_minutes':10, 'initial_crowd':0})
        self.assertAlmostEqual(estimate['peak'], 20)
        self.assertAlmostEqual(estimate['final'], 20)
        initial = occupancy_estimate(self.task, result, {'dwell_minutes':10, 'initial_crowd':150})
        self.assertAlmostEqual(initial['peak'], 150)
        self.assertAlmostEqual(initial['final'], 20)
        self.assertIsNone(occupancy_estimate({}, {'status':'empty'}, {}))


if __name__ == '__main__':
    unittest.main()
