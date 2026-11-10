"""Observed population inputs share the review, native preflight and run path."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from postanalysis_api.schemas.common import ApiError
from postanalysis_api.services.population_execution import prepare_execution_dataset, population_cohorts, execution_context
from postanalysis_api.services.execution_template import execution_template
from postanalysis_api.workers.memorial_scenario import compile_routes
from test_post_unified_design import fixture


def population_dataset(duration=600):
    return {'id': 'ds-population', 'version': 'b' * 64, 'metadata': {}, 'observations': [
        {'regionId': 'global', 'timeSeconds': second, 'population': count, 'density': .1, 'meanSpeed': .9, 'pressureProxy': None}
        for second, count in ((.5, 20), (duration / 2, 30), (duration, 10))]}


class PopulationExecutionTests(unittest.TestCase):
    def test_unified_counterfactual_runs_can_enter_the_real_mongo_archive_writer(self):
        from unittest.mock import MagicMock
        from postanalysis_api.repositories.mongodb import MongoRepository
        repository = MongoRepository.__new__(MongoRepository)
        repository.db = MagicMock()
        configuration = {'planId': 'CF-01', 'seed': 1, 'scenario': {'durationSeconds': 600}}
        archive = repository.begin_archive('run-cf', 'counterfactual', 'u', 'w', configuration)
        self.assertEqual('counterfactual', archive['experimentType'])
        self.assertEqual('run-cf', archive['runId'])
        self.assertEqual(configuration, repository.db[archive['collections']['detail']].insert_one.call_args.args[0]['configuration'])
        with self.assertRaises(ValueError):
            repository.begin_archive('bad', 'undefined', 'u', 'w', {})

    def test_duration_controls_and_analysis_windows_follow_loaded_data(self):
        for duration in (2, 3, 60, 600, 1800, 24000, 86400):
            with self.subTest(duration=duration):
                dataset = population_dataset(duration); before = deepcopy(dataset)
                template = execution_template(dataset)
                self.assertEqual(duration, template['scenario']['durationSeconds'])
                self.assertEqual(duration, template['pcFeatures'][-1]['windowEndSeconds'])
                self.assertTrue(all(0 <= f['windowStartSeconds'] < f['windowEndSeconds'] <= duration for f in template['pcFeatures']))
                self.assertTrue(all(action['atSeconds'] < duration for group in template['groups'] for action in group['interventions']))
                self.assertEqual(before, dataset)

    def test_population_changes_create_shared_deterministic_people_without_mutating_observations(self):
        dataset = prepare_execution_dataset(population_dataset())
        cohorts = population_cohorts(dataset)
        self.assertEqual(30, len(cohorts))
        self.assertEqual(20, sum(person['depart'] == 0 for person in cohorts))
        self.assertEqual(20, sum(person['release'] == 600 for person in cohorts))
        self.assertEqual(cohorts, population_cohorts(dataset))
        self.assertEqual('b' * 64, dataset['metadata']['reconstruction']['sourceDatasetVersion'])
        invalid = population_dataset(); invalid['observations'][0]['population'] = 20001
        with self.assertRaises(ApiError):
            prepare_execution_dataset(invalid)

    def test_current_review_maps_exactly_to_sumo_routes_and_one_factor_references(self):
        dataset = population_dataset(); _, plan = fixture()
        context = execution_context(dataset)
        plan['factualBaseline'].update(datasetId=dataset['id'], datasetVersion=dataset['version'])
        plan['counterfactualDesign'].update(baselineExecution=context['baselineExecution'], durationSeconds=600)
        groups = plan['counterfactualDesign']['groups'][:3]
        for group, values, factors in zip(groups, ((300, .7, 0), (300, .4, 30), (360, .7, 30)),
                                          (['T_start', 'Compliance', 'Delay'], ['T_start'], ['Compliance'])):
            group['execution'].update(startSeconds=values[0], compliance=values[1], delaySeconds=values[2], referenceDesignId='F-00')
            group['factorSymbols'] = factors
        plan['counterfactualDesign']['groups'] = groups
        defaults = execution_template(dataset)
        plan['executionSettings']['pcFeatures'] = defaults['pcFeatures']
        plan['counterfactualDesign']['executionSettings'] = deepcopy(plan['executionSettings'])
        task = execution_template(dataset, plan)
        from postanalysis_api.workers.analysis import summarize
        from postanalysis_api.workers.memorial_scenario import control_schedule
        base_controls = prepare_execution_dataset(dataset)['metadata']['executionProfile']['baselineControl']
        task['reviewSnapshot'] = plan
        runs = [{'id': 'test-' + group_id, 'planId': group_id, 'seed': 1, 'status': 'completed',
                 'controls': control_schedule(actions, base_controls), 'metrics': {metric: 1 for metric in task['metricIds']}}
                for group_id, actions in [('F-00', []), *[(group['id'], group['interventions']) for group in task['groups']]]]
        analysis = summarize(task, runs)
        self.assertEqual({'CF-02', 'CF-03'}, {item['groupId'] for item in analysis['intervention']['experiments']})
        with TemporaryDirectory() as folder:
            baseline = compile_routes(dataset, 42, [], 600, Path(folder) / 'factual.xml')
            candidate = compile_routes(dataset, 42, task['groups'][1]['interventions'], 600, Path(folder) / 'earlier.xml')
            self.assertEqual(360, baseline['controls']['gateOpenSeconds'])
            self.assertEqual(30, baseline['controls']['guidanceDelaySeconds'])
            self.assertEqual(300, candidate['controls']['gateOpenSeconds'])
            self.assertEqual(.4, candidate['controls']['compliance'])
            self.assertEqual([p['id'] for p in baseline['cohorts']], [p['id'] for p in candidate['cohorts']])
        plan['executionSettings']['pcFeatures'][0]['windowEndSeconds'] = 601
        with self.assertRaises(ApiError):
            execution_template(dataset, plan)
