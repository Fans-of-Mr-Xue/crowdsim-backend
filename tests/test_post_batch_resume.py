"""Interrupted post batches resume the original group/seed matrix without data loss."""
from copy import deepcopy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

import mongomock

from postanalysis_api.api.common import Request
from postanalysis_api.api.experiments import handle
from postanalysis_api.repositories.mongodb import MongoRepository
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.services.application import PostAnalysisApp
from postanalysis_api.workers.batch_state import SupersededExecution, completed_batch_runs, run_matrix
from postanalysis_api.workers.sumo_batch import SumoBatchExecutor


def fixture(status='interrupted'):
    repo = MongoRepository.__new__(MongoRepository)
    repo.client, repo.db, repo._indexes_ready = None, mongomock.MongoClient().db, True
    task = {'id': 'exp_resume', 'ownerId': 'u', 'workspaceId': 'post-c', 'datasetId': 'ds_1',
            'datasetVersion': 'v1', 'status': status, 'createdAt': '2026-11-12T10:00:00Z',
            'scenario': {'durationSeconds': 10}, 'seedStart': 7, 'metricIds': ['risk_exposure'], 'pcFeatures': [],
            'groups': [{'id': identity, 'name': identity, 'hypothesis': '比较风险暴露', 'experimentType': 'counterfactual',
                        'repeatCount': 2, 'referenceGroupId': 'F-00', 'interventions': []} for identity in ('CF-01', 'CF-02')],
            'progress': {'baselinePlanned': 2, 'baselineCompleted': 0, 'interventionPlanned': 4,
                         'interventionCompleted': 0, 'failed': 1,
                         'groups': [{'groupId': identity, 'planned': 2, 'completed': 0, 'failed': 0} for identity in ('CF-01', 'CF-02')]},
            'error': '原失败原因'}
    archive = repo.begin_archive(task['id'], 'experiment', 'u', 'post-c', task)
    task['archive'] = archive
    repo.finish_archive(archive, status, {})
    repo.db.postanalysis_experiments.insert_one(deepcopy(task))
    repo.db.postanalysis_datasets.insert_one({'id': 'ds_1', 'ownerId': 'u', 'workspaceId': 'post-c', 'version': 'v1'})
    for identity, plan, seed, run_status in (('old-base', 'F-00', 7, 'completed'), ('old-cf', 'CF-02', 8, 'completed'), ('failed-attempt', 'F-00', 8, 'failed')):
        repo.db.postanalysis_runs.insert_one({'id': identity, 'experimentId': task['id'], 'planId': plan, 'seed': seed,
            'status': run_status, 'ownerId': 'u', 'workspaceId': 'post-c', 'datasetVersion': 'v1',
            'scenario': deepcopy(task['scenario']), 'metrics': {'risk_exposure': 1}, 'createdAt': identity})
    app = PostAnalysisApp.__new__(PostAnalysisApp)
    app.repo = repo
    app.executor = SimpleNamespace(is_active=Mock(return_value=False), submit=Mock())
    app._check_start_capability = Mock()
    return app, repo, task


class PostBatchResumeTests(unittest.TestCase):
    def test_resuming_keeps_the_batch_and_restores_counts_from_completed_runs_not_stale_progress(self):
        app, repo, task = fixture()
        old = list(repo.db.postanalysis_runs.find({}))
        result = app.resume('experiments', task['id'], 'u', 'post-c')
        self.assertEqual(task['id'], result['id'])
        self.assertEqual('queued', result['status'])
        self.assertEqual(1, result['progress']['baselineCompleted'])
        self.assertEqual(1, result['progress']['interventionCompleted'])
        self.assertEqual([0, 1], [row['completed'] for row in result['progress']['groups']])
        self.assertEqual(0, result['progress']['failed'])
        self.assertEqual(old, list(repo.db.postanalysis_runs.find({})))
        self.assertEqual(1, repo.db.postanalysis_experiments.count_documents({}))
        self.assertEqual(1, result['resumeCount'])
        self.assertEqual('resumed', repo.events('experiments', task['id'], 'u', 'post-c', 0)['events'][0]['type'])

    def test_repeat_resume_is_idempotent_and_wrong_owners_or_states_cannot_start_work(self):
        app, repo, task = fixture()
        headers = {'X-CrowdSim-User': 'u', 'X-CrowdSim-Workspace': 'post-c', 'Idempotency-Key': 'resume-request'}
        request = Request('POST', f"/experiments/{task['id']}/resume", {}, {}, headers, app)
        self.assertEqual(202, handle(request)[0])
        self.assertTrue(handle(request)[2])
        self.assertEqual(1, app.executor.submit.call_count)
        with self.assertRaises(ApiError):
            app.resume('experiments', task['id'], 'u', 'post-c')
        with self.assertRaises(ApiError):
            app.resume('experiments', task['id'], 'other', 'post-c')
        for status in ('draft', 'completed', 'running', 'queued', 'cancelling'):
            app, repo, task = fixture(status)
            with self.subTest(status=status), self.assertRaises(ApiError):
                app.resume('experiments', task['id'], 'u', 'post-c')
            app.executor.submit.assert_not_called()

    def test_active_worker_changed_data_or_duplicate_completed_seed_are_rejected(self):
        for condition in ('active', 'version', 'duplicate'):
            app, repo, task = fixture()
            if condition == 'active':
                app.executor.is_active.return_value = True
            elif condition == 'version':
                repo.db.postanalysis_datasets.update_one({'id': 'ds_1'}, {'$set': {'version': 'v2'}})
            else:
                duplicate = repo.db.postanalysis_runs.find_one({'id': 'old-base'})
                duplicate.pop('_id'); duplicate['id'] = 'duplicate'
                repo.db.postanalysis_runs.insert_one(duplicate)
            with self.subTest(condition=condition), self.assertRaises(ApiError):
                app.resume('experiments', task['id'], 'u', 'post-c')
            self.assertEqual('interrupted', repo.get('experiments', task['id'], 'u', 'post-c')['status'])
            app.executor.submit.assert_not_called()

    def test_submission_failure_restores_the_previous_status_and_error(self):
        app, repo, task = fixture()
        app.executor.submit.side_effect = RuntimeError('submission failed')
        with self.assertRaises(RuntimeError):
            app.resume('experiments', task['id'], 'u', 'post-c')
        stored = repo.get('experiments', task['id'], 'u', 'post-c')
        self.assertEqual(task['status'], stored['status'])
        self.assertEqual(task['progress'], stored['progress'])
        self.assertEqual(task['error'], stored['error'])
        app.executor.submit.side_effect = None
        self.assertEqual(1, app.resume('experiments', task['id'], 'u', 'post-c')['resumeCount'])

    def test_old_worker_is_fenced_before_writing_a_resumed_attempt(self):
        app, repo, task = fixture()
        resumed = app.resume('experiments', task['id'], 'u', 'post-c')
        with self.assertRaises(SupersededExecution):
            app.record_worker_event('experiments', task['id'], 'u', 'post-c', {'status': 'running'}, execution_attempt='old-worker')
        self.assertEqual(resumed, repo.get('experiments', task['id'], 'u', 'post-c'))

    def test_attempt_change_during_compare_and_set_cannot_overwrite_the_new_worker(self):
        app, repo, task = fixture('running')
        repo.db.postanalysis_experiments.update_one({'id': task['id']}, {'$set': {'executionAttemptId': 'old'}})
        transition = repo.transition_execution
        def race(*args, **kwargs):
            repo.db.postanalysis_experiments.update_one({'id': task['id']}, {'$set': {'executionAttemptId': 'new'}})
            return transition(*args, **kwargs)
        with patch.object(repo, 'transition_execution', side_effect=race), self.assertRaises(SupersededExecution):
            app.record_worker_event('experiments', task['id'], 'u', 'post-c', {'status': 'running'}, execution_attempt='old')
        self.assertEqual('new', repo.get('experiments', task['id'], 'u', 'post-c')['executionAttemptId'])

    def test_worker_only_runs_missing_seeds_reuses_the_archive_and_combines_old_and_new_results(self):
        app, repo, task = fixture()
        old_completed = list(repo.db.postanalysis_runs.find({'status': 'completed'}))
        resumed = app.resume('experiments', task['id'], 'u', 'post-c')
        with TemporaryDirectory() as folder:
            executor = SumoBatchExecutor(repo, folder)
            app.executor = executor
            calls = []
            def one(task, dataset, plan, kind, seed, actions, cancel, **kwargs):
                calls.append((plan, seed, kwargs['run_index']))
                return repo.create('runs', 'u', 'post-c', {'experimentId': task['id'], 'planId': plan, 'seed': seed,
                    'datasetVersion': 'v1', 'scenario': task['scenario'], 'status': 'completed', 'metrics': {'risk_exposure': 2}})
            with patch.object(executor, '_one', side_effect=one):
                executor._run(resumed, {}, lambda event: app.record_worker_event('experiments', task['id'], 'u', 'post-c', event,
                              execution_attempt=resumed['executionAttemptId']), threading.Event())
        self.assertEqual([('F-00', 8, 2), ('CF-01', 7, 3), ('CF-01', 8, 4), ('CF-02', 7, 5)], calls)
        self.assertEqual(old_completed, list(repo.db.postanalysis_runs.find({'id': {'$in': ['old-base', 'old-cf']}})))
        final = repo.get('experiments', task['id'], 'u', 'post-c')
        self.assertEqual('completed', final['status'])
        self.assertEqual(2, final['progress']['baselineCompleted'])
        self.assertEqual(4, final['progress']['interventionCompleted'])
        self.assertEqual(1, repo.db.post.count_documents({'runId': task['id']}))
        result = repo.get('results', final['resultId'], 'u', 'post-c')
        self.assertEqual(6, len(result['metrics']['runs']))
        self.assertEqual({2}, {row['pairCount'] for row in result['comparison']['effects']})

    def test_a_second_interruption_recovers_newly_completed_runs_even_if_progress_was_not_saved(self):
        app, repo, task = fixture()
        first = app.resume('experiments', task['id'], 'u', 'post-c')
        repo.create('runs', 'u', 'post-c', {'experimentId': task['id'], 'planId': 'F-00', 'seed': 8,
                    'datasetVersion': 'v1', 'status': 'completed', 'metrics': {'risk_exposure': 2}})
        repo.update('experiments', task['id'], 'u', 'post-c', {'status': 'interrupted'})
        second = app.resume('experiments', task['id'], 'u', 'post-c')
        self.assertEqual(2, second['resumeCount'])
        self.assertNotEqual(first['executionAttemptId'], second['executionAttemptId'])
        self.assertEqual(2, second['progress']['baselineCompleted'])
        self.assertEqual(1, second['progress']['interventionCompleted'])

    def test_a_batch_with_all_runs_archived_can_finish_without_rerunning_sumo(self):
        app, repo, task = fixture()
        existing = completed_batch_runs(repo, task)
        for plan, _kind, seed, _actions in run_matrix(task):
            if (plan, seed) not in existing:
                repo.create('runs', 'u', 'post-c', {'experimentId': task['id'], 'planId': plan, 'seed': seed,
                    'datasetVersion': 'v1', 'status': 'completed', 'metrics': {'risk_exposure': 2}})
        resumed = app.resume('experiments', task['id'], 'u', 'post-c')
        with TemporaryDirectory() as folder:
            executor = SumoBatchExecutor(repo, folder)
            with patch.object(executor, '_one') as one:
                executor._run(resumed, {}, lambda event: app.record_worker_event('experiments', task['id'], 'u', 'post-c', event,
                              execution_attempt=resumed['executionAttemptId']), threading.Event())
            one.assert_not_called()
        self.assertEqual('completed', repo.get('experiments', task['id'], 'u', 'post-c')['status'])

    def test_completed_queries_ignore_failed_attempts_even_past_the_old_650_record_limit(self):
        _app, repo, task = fixture()
        for index in range(660):
            repo.db.postanalysis_runs.insert_one({'id': str(index), 'experimentId': task['id'], 'ownerId': 'u',
                'workspaceId': 'post-c', 'status': 'failed', 'createdAt': 'earlier'})
        self.assertEqual(2, len(completed_batch_runs(repo, task)))
        self.assertEqual(663, len(repo.runs_for_experiment(task['id'], 'u', 'post-c')))

    def test_constructing_an_application_does_not_reconcile_active_jobs(self):
        repository = Mock()
        with patch('postanalysis_api.services.application.MongoRepository', return_value=repository):
            app = PostAnalysisApp('unused', executor=Mock())
        repository.reconcile_interrupted.assert_not_called()
        app.recover_interrupted()
        repository.reconcile_interrupted.assert_called_once()


if __name__ == '__main__':
    unittest.main()
