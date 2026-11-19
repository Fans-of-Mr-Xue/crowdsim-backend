"""A live pause keeps the execution attempt and remains cancellable on shutdown."""
import threading
import unittest
from collections import OrderedDict
from unittest.mock import Mock

from tests.test_post_batch_resume import fixture
from postanalysis_api.workers.sumo_batch import SumoBatchExecutor
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.api.common import Request
from postanalysis_api.api.experiments import handle


class PostBatchPauseTests(unittest.TestCase):
    def setup_task(self):
        app, repo, task = fixture('running')
        executor = SumoBatchExecutor.__new__(SumoBatchExecutor)
        executor._lock = threading.RLock()
        executor._runtime = OrderedDict()
        executor._jobs = {task['id']: threading.Event()}
        executor._pause_requests = {task['id']: threading.Event()}
        executor._threads = {}
        app.executor = executor
        return app, repo, task, executor

    def test_pause_acknowledges_at_checkpoint_and_continue_keeps_original_attempt(self):
        app, repo, task, executor = self.setup_task()
        original_runs = list(repo.db.postanalysis_runs.find())
        app.pause('experiments', task['id'], 'u', 'post-c')
        self.assertEqual('pausing', repo.get('experiments', task['id'], 'u', 'post-c')['status'])
        acknowledged, continued = threading.Event(), threading.Event()
        def event(value):
            app.record_worker_event('experiments', task['id'], 'u', 'post-c', value)
            acknowledged.set()
        worker = threading.Thread(target=lambda: (executor._checkpoint(task, event, executor._jobs[task['id']]), continued.set()))
        executor._threads[task['id']] = worker
        worker.start()
        try:
            self.assertTrue(acknowledged.wait(2))
            self.assertEqual('paused', repo.get('experiments', task['id'], 'u', 'post-c')['status'])
            self.assertFalse(continued.wait(.1))
            result = app.resume('experiments', task['id'], 'u', 'post-c')
            self.assertEqual('running', result['status'])
            self.assertNotIn('resumeCount', result)
            self.assertTrue(continued.wait(2))
            self.assertEqual(original_runs, list(repo.db.postanalysis_runs.find()))
        finally:
            executor.close()

    def test_shutdown_and_cancel_release_a_paused_worker(self):
        for operation in ('cancel', 'close'):
            with self.subTest(operation=operation):
                app, repo, task, executor = self.setup_task()
                app.pause('experiments', task['id'], 'u', 'post-c')
                acknowledged, stopped = threading.Event(), threading.Event()
                def event(value):
                    app.record_worker_event('experiments', task['id'], 'u', 'post-c', value)
                    acknowledged.set()
                def work():
                    try:
                        executor._checkpoint(task, event, executor._jobs[task['id']])
                    except InterruptedError:
                        stopped.set()
                worker = threading.Thread(target=work)
                executor._threads[task['id']] = worker
                worker.start()
                self.assertTrue(acknowledged.wait(2))
                if operation == 'cancel':
                    self.assertEqual('cancelling', app.cancel('experiments', task['id'], 'u', 'post-c')['status'])
                else:
                    executor.close()
                self.assertTrue(stopped.wait(2))
                worker.join(2)
                self.assertFalse(worker.is_alive())

    def test_pause_state_survives_progress_and_status_race(self):
        app, repo, task, executor = self.setup_task()
        transition = repo.transition_task
        def pause_before_cas(*args, **kwargs):
            app.pause('experiments', task['id'], 'u', 'post-c')
            repo.transition_task = transition
            return transition(*args, **kwargs)
        repo.transition_task = pause_before_cas
        app.record_worker_event('experiments', task['id'], 'u', 'post-c', {'status': 'running'})
        self.assertEqual('pausing', repo.get('experiments', task['id'], 'u', 'post-c')['status'])
        with self.assertRaises(ApiError):
            app.resume('experiments', task['id'], 'u', 'post-c')

    def test_restart_reconciles_paused_batches_to_existing_archive_resume(self):
        app, repo, task, executor = self.setup_task()
        repo.db.postanalysis_experiments.update_one({'id': task['id']}, {'$set': {'status': 'paused'}})
        repo.reconcile_interrupted()
        self.assertEqual('interrupted', repo.get('experiments', task['id'], 'u', 'post-c')['status'])
        app.executor = Mock(is_active=Mock(return_value=False))
        resumed = app.resume('experiments', task['id'], 'u', 'post-c')
        self.assertEqual(1, resumed['progress']['baselineCompleted'])
        self.assertEqual(1, resumed['progress']['interventionCompleted'])
        self.assertEqual(task['id'], resumed['id'])

    def test_http_pause_and_continue_retries_are_idempotent(self):
        app, repo, task, executor = self.setup_task()
        def request(action, key):
            return Request('POST', f"/experiments/{task['id']}/{action}", {}, {},
                           {'X-CrowdSim-User': 'u', 'X-CrowdSim-Workspace': 'post-c', 'Idempotency-Key': key}, app)
        paused = handle(request('pause', 'pause-once'))
        self.assertEqual(202, paused[0])
        self.assertEqual('pausing', paused[1]['status'])
        self.assertTrue(handle(request('pause', 'pause-once'))[2])
        app.record_worker_event('experiments', task['id'], 'u', 'post-c', {'status': 'paused'})
        continued = handle(request('resume', 'continue-once'))
        self.assertEqual('running', continued[1]['status'])
        self.assertFalse(executor._pause_requests[task['id']].is_set())
        self.assertTrue(handle(request('resume', 'continue-once'))[2])
