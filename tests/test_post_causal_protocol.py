"""Primary endpoint inference and health checks never manufacture evidence."""
from types import SimpleNamespace
import json
from pathlib import Path
import unittest
from pymongo.errors import ServerSelectionTimeoutError
from postanalysis_api.services.application import PostAnalysisApp
from postanalysis_api.workers.analysis import paired_effect, assess_hypothesis
from postanalysis_api.workers.analysis import summarize
from postanalysis_api.services.execution_template import execution_template
from postanalysis_api.schemas.review_intake import validate_review_intake
from postanalysis_api.workers.causal import discover
import random


class CausalProtocolTests(unittest.TestCase):
    def test_pc_orients_a_gaussian_collider_and_rejects_duplicate_seeds(self):
        rng = random.Random(31)
        features = [{'id': key, 'metricId': 'mean_speed', 'regionId': 'global', 'windowStartSeconds': 0, 'windowEndSeconds': 1} for key in ('x', 'y', 'z')]
        runs = []
        for seed in range(500):
            x, z = rng.gauss(0, 1), rng.gauss(0, 1)
            runs.append({'planId': 'F-00', 'seed': seed, 'status': 'completed', 'featureValues': {'x': x, 'y': x + z + rng.gauss(0, .2), 'z': z}})
        graph = discover(features, runs)
        self.assertEqual('computed', graph['status'])
        self.assertEqual({('x', 'y'), ('z', 'y')}, {(edge['source'], edge['target']) for edge in graph['edges'] if edge['orientation'] == 'directed'})
        self.assertEqual('invalid_data', discover(features, runs + [runs[0]])['status'])

    def test_frontend_live_preset_compiles_to_eighteen_valid_bound_runs(self):
        root = Path(__file__).resolve().parents[1]
        plan = json.loads((root / 'docs/examples/shanghai_memorial_post_plan.json').read_text(encoding='utf-8'))
        dataset = json.loads((root / 'docs/examples/shanghai_memorial_post_dataset.json').read_text(encoding='utf-8'))
        dataset.update(id='ds-validation', version='a' * 64)
        plan['factualBaseline'].update(datasetId=dataset['id'], datasetVersion=dataset['version'])
        validate_review_intake(plan)
        execution = execution_template(dataset, plan)
        self.assertEqual(19800, execution['scenario']['durationSeconds'])
        self.assertEqual(18, max(g['repeatCount'] for g in execution['groups']) + sum(g['repeatCount'] for g in execution['groups']))
        self.assertEqual(['F-00', 'CF-01', 'CF-01', 'CF-01', 'CF-01'], [g['referenceGroupId'] for g in execution['groups']])
        self.assertTrue(all(g['experimentType'] == 'counterfactual' for g in execution['groups']))

    def test_nonfinite_bool_and_unpaired_samples_are_excluded(self):
        runs = [{"seed": seed, "planId": group, "status": "completed", "metrics": {"mean_speed": value}}
                for seed, group, value in [(1, "F-00", 1), (1, "CF", 2), (2, "F-00", float('nan')),
                                           (2, "CF", 9), (3, "F-00", True), (3, "CF", float('inf'))]]
        effect = paired_effect(runs, 'F-00', 'CF', 'mean_speed')
        self.assertEqual([1], effect['seeds'])
        self.assertEqual(1, effect['estimate'])
        self.assertEqual(1, effect['excludedUnpairedCount'])
        with self.assertRaisesRegex(ValueError, '唯一'):
            paired_effect(runs + [runs[0]], 'F-00', 'CF', 'mean_speed')

    def test_placebo_checks_each_seed_rather_than_cancelling_opposite_errors(self):
        actions = [{'id': 'open', 'targetRegionId': 'C', 'actionType': 'open_gate', 'atSeconds': 14700, 'parameters': {}},
                   {'id': 'guide', 'targetRegionId': 'C', 'actionType': 'guidance', 'atSeconds': 14700, 'parameters': {'compliance': .4, 'delaySeconds': 1500}}]
        task = {'metricIds': ['risk_exposure'], 'pcFeatures': [], 'groups': [{'id': 'CF', 'name': 'placebo', 'hypothesis': 'same', 'referenceGroupId': 'F-00', 'experimentType': 'counterfactual', 'interventions': actions}]}
        runs = [{'id': f'{group}-{seed}', 'planId': group, 'seed': seed, 'status': 'completed', 'metrics': {'risk_exposure': value}}
                for seed, group, value in [(1, 'F-00', 10), (2, 'F-00', 10), (1, 'CF', 9), (2, 'CF', 11)]]
        result = summarize(task, runs)
        self.assertEqual(0, result['comparison']['effects'][0]['estimate'])
        self.assertEqual('failed', result['observation']['experiments'][0]['placeboCheck']['status'])
        self.assertTrue(any('不代表' in note for note in result['diagnostics']))
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        result.update(id='result-1', datasetVersion='version-1')
        task_record = {'status': 'completed', 'resultId': 'result-1', 'datasetVersion': 'version-1'}
        app.repo = SimpleNamespace(get=lambda kind, *args: task_record if kind == 'experiments' else result)
        response = app.result('exp-1', 'u', 'w', 'observation')
        self.assertEqual(result['diagnostics'], response['diagnostics'])
        self.assertEqual(result['observation'], response['data'])
        del result['diagnostics']
        self.assertEqual(result['limitations'], app.result('exp-1', 'u', 'w', 'observation')['diagnostics'])

    def test_primary_outcome_direction_sample_size_and_missingness_control_verdict(self):
        design = {"primaryMetricId": "risk_exposure", "expectedDirection": "decrease"}
        effect = {"metricId": "risk_exposure", "estimate": -2, "ci95": [-3, -1], "pairCount": 20}
        assess = lambda changes: assess_hypothesis(design, [dict(effect, **changes)])["status"]
        self.assertEqual('supported', assess({}))
        self.assertEqual('opposed', assess({'estimate': 2, 'ci95': [1, 3]}))
        self.assertEqual('inconclusive', assess({'ci95': [-3, 1]}))
        self.assertEqual('descriptive_only', assess({'pairCount': 3}))
        self.assertEqual('missing_data', assess({'estimate': None}))

    def test_health_reports_unavailable_storage_without_credentials_or_fallback(self):
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        def command(_):
            raise ServerSelectionTimeoutError('private connection detail')
        app.repo = SimpleNamespace(db=SimpleNamespace(command=command))
        app.executor = SimpleNamespace(capabilities=lambda: {'ready': True})
        result = app.health()
        self.assertFalse(result['ready'])
        self.assertEqual({'ready': False, 'kind': 'mongodb', 'reason': 'STORAGE_UNAVAILABLE'}, result['storage'])
        self.assertNotIn('private', str(result))


if __name__ == '__main__':
    unittest.main()
