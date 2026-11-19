"""Browser input stays proportional to groups, with one selected run fetched separately."""
import json
import unittest
import mongomock

from postanalysis_api.repositories.mongodb import MongoRepository
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.services.analysis_data import prepare_analysis, run_series


class PostAnalysisDataTests(unittest.TestCase):
    def setUp(self):
        self.repo = MongoRepository.__new__(MongoRepository)
        self.repo.db = mongomock.MongoClient().db
        self.repo._indexes_ready = True
        self.task = {'id': 'exp_one', 'ownerId': 'u', 'workspaceId': 'w', 'status': 'completed',
                     'resultId': 'r1', 'datasetVersion': 'v1', 'groups': [{'id': 'CF-01'}]}
        self.repo.db.postanalysis_experiments.insert_one(self.task)
        self.repo.db.postanalysis_results.insert_one({'id': 'r1', 'ownerId': 'u', 'workspaceId': 'w',
            'datasetVersion': 'v1', 'observation': {}, 'intervention': {}, 'mechanism': {}, 'causalGraph': {}})

    def add(self, group, seed, timeline):
        self.repo.db.postanalysis_runs.insert_one({'id': f'{group}-{seed}', 'experimentId': 'exp_one',
            'ownerId': 'u', 'workspaceId': 'w', 'planId': group, 'seed': seed, 'status': 'completed',
            'datasetVersion': 'v1', 'timeline': timeline, 'metrics': {'mean_speed': 0},
            'controlEvents': ['large event'] * 100, 'calibration': {'populationResiduals': ['large residual'] * 100}})

    def test_exact_means_nulls_zeros_probabilities_and_complete_time_range_are_aggregated_on_server(self):
        self.add('F-00', 1, [{'timeSeconds': 0, 'density': 4, 'meanSpeed': 0, 'population': 10},
                             {'timeSeconds': 5, 'density': 2, 'meanSpeed': None, 'population': 0}])
        self.add('F-00', 2, [{'timeSeconds': 0, 'density': 0, 'meanSpeed': None, 'population': 20},
                             {'timeSeconds': 5, 'density': 6, 'meanSpeed': None, 'population': 0}])
        result = prepare_analysis(self.repo, 'exp_one', 'u', 'w')
        prepared = result['prepared']
        self.assertEqual([0, 5], prepared['seconds'])
        self.assertEqual(2, prepared['rowsByGroup']['F-00'][0]['density'])
        self.assertEqual(0, prepared['rowsByGroup']['F-00'][0]['meanSpeed'])
        self.assertIsNone(prepared['rowsByGroup']['F-00'][1]['meanSpeed'])
        self.assertEqual(50, prepared['baselineProbabilities']['0']['stable'])
        self.assertEqual(50, prepared['finalProbabilities']['F-00']['recovering'])
        self.assertEqual(50, prepared['finalProbabilities']['F-00']['unstable'])
        for run in result['runs']:
            self.assertEqual(2, run['timelineCount'])
            self.assertFalse({'timeline', 'controlEvents', 'calibration'} & run.keys())
        series = run_series(self.repo, 'F-00-1', 'u', 'w')
        self.assertEqual(2, len(series['timeline']))
        self.assertNotIn('controlEvents', series)

    def test_many_seeds_do_not_multiply_browser_timeline_storage(self):
        timeline = [{'timeSeconds': index / 2, 'density': 0, 'meanSpeed': 0, 'pressureProxy': 0, 'population': 600} for index in range(1200)]
        for seed in range(30):
            self.add('F-00', seed, timeline)
            self.add('CF-01', seed, timeline)
        result = prepare_analysis(self.repo, 'exp_one', 'u', 'w')
        self.assertEqual(60, len(result['runs']))
        self.assertEqual(2400, sum(len(rows) for rows in result['prepared']['rowsByGroup'].values()))
        self.assertLess(len(json.dumps(result)), 700000)
        self.assertTrue(all('timeline' not in run for run in result['runs']))

    def test_wrong_scope_incomplete_batch_and_mismatched_versions_are_rejected(self):
        with self.assertRaises(ApiError):
            prepare_analysis(self.repo, 'exp_one', 'someone-else', 'w')
        self.add('F-00', 1, [])
        with self.assertRaises(ApiError):
            run_series(self.repo, 'F-00-1', 'u', 'wrong-workspace')
        self.repo.db.postanalysis_runs.update_one({'id': 'F-00-1'}, {'$set': {'datasetVersion': 'wrong'}})
        with self.assertRaises(ApiError):
            prepare_analysis(self.repo, 'exp_one', 'u', 'w')
        self.repo.db.postanalysis_experiments.update_one({'id': 'exp_one'}, {'$set': {'status': 'running'}})
        with self.assertRaises(ApiError):
            prepare_analysis(self.repo, 'exp_one', 'u', 'w')
