"""Real isolated worker/process cleanup plus exceptional initialization paths."""
import asyncio
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import psutil
from aiohttp import web
import start_backend
from backend_process_job import WindowsProcessJob
from data_service.api.pre_assessments import shutdown_pool
from postanalysis_api.services.application import PostAnalysisApp
from postanalysis_api.workers.sumo_batch import SumoBatchExecutor
from crowdsim.infrastructure.websocket_server import OverlayServer


def hold_worker(path):
    allocation = bytearray(8 * 1024 * 1024)
    temporary = Path(path + '.tmp')
    temporary.write_text(str(os.getpid()), encoding='utf-8')
    temporary.replace(path)
    while allocation:
        time.sleep(1)


class ResourceReleaseTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows Job handles')
    def test_job_handles_are_released_after_repeated_close(self):
        warmup = WindowsProcessJob()
        warmup.close()
        process = psutil.Process()
        before = process.num_handles()
        for _ in range(20):
            job = WindowsProcessJob()
            job.close()
            job.close()
            self.assertIsNone(job.handle)
        self.assertLessEqual(process.num_handles(), before + 1)

    def test_live_process_pool_is_stopped_and_process_handles_are_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.pid'
            pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn'))
            future = pool.submit(hold_worker, str(path))
            processes = []
            try:
                deadline = time.monotonic() + 15
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(path.exists(), 'isolated worker did not start')
                pid = int(path.read_text(encoding='utf-8'))
                processes = list(pool._processes.values())
                self.assertTrue(psutil.pid_exists(pid))
                shutdown_pool(pool)
                self.assertFalse(psutil.pid_exists(pid))
                self.assertTrue(future.done())
                self.assertTrue(all(process._closed for process in processes))
                self.assertIsNone(pool._executor_manager_thread)
            finally:
                if pool._processes:
                    shutdown_pool(pool)

    def assert_tree_released(self, parent_exits=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'child.json'
            program = ("import json,os,socket,time; from pathlib import Path; "
                       "allocation=bytearray(8*1024*1024); s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
                       f"p=Path({str(path)!r}); t=p.with_suffix('.tmp'); "
                       "t.write_text(json.dumps([os.getpid(),s.getsockname()[1]])); t.replace(p); time.sleep(120)")
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{program!r}]); time.sleep(120)"
            supervisor = start_backend.Supervisor()
            self.addCleanup(supervisor.close)
            child = supervisor.spawn('isolated resource test', [sys.executable, '-c', parent])
            try:
                deadline = time.monotonic() + 15
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(path.exists())
                descendant, port = json.loads(path.read_text())
                with socket.create_connection(('127.0.0.1', port), timeout=2):
                    pass
                if parent_exits:
                    child.kill()
                    child.wait(timeout=5)
                    self.assertTrue(psutil.pid_exists(descendant))
                supervisor.close()
                self.assertIsNotNone(child.poll())
                self.assertFalse(psutil.pid_exists(descendant))
                with socket.socket() as probe:
                    probe.settimeout(1)
                    self.assertNotEqual(0, probe.connect_ex(('127.0.0.1', port)))
                self.assertFalse(supervisor.children)
                self.assertFalse(supervisor.managed)
                self.assertFalse(supervisor.jobs)
            finally:
                supervisor.close()

    def test_supervisor_releases_owned_parent_descendant_and_listening_port(self):
        self.assert_tree_released()

    @unittest.skipUnless(os.name == 'nt', 'Windows Job ownership')
    def test_child_stays_owned_after_its_service_parent_crashes(self):
        self.assert_tree_released(parent_exits=True)

    @unittest.skipUnless(os.name == 'nt', 'Windows Job ownership')
    def test_optional_service_cleanup_does_not_close_other_service_jobs(self):
        supervisor = start_backend.Supervisor()
        self.addCleanup(supervisor.close)
        first = supervisor.spawn('failed optional fixture', [sys.executable, '-c', 'import time; time.sleep(120)'])
        second = supervisor.spawn('healthy fixture', [sys.executable, '-c', 'import time; time.sleep(120)'])
        first.kill(); first.wait(timeout=5)
        supervisor._discard_children([supervisor.children[0]])
        self.assertIsNone(second.poll())
        self.assertNotIn(first.pid, supervisor.jobs)
        self.assertIn(second.pid, supervisor.jobs)
        supervisor.close()
        self.assertIsNotNone(second.poll())

    @unittest.skipUnless(os.name == 'nt', 'Windows Job ownership')
    def test_abrupt_launcher_exit_releases_its_owned_job_and_port(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'owned.json'
            child_program = ("import os,json,socket,time; from pathlib import Path; s=socket.socket(); "
                             "s.bind(('127.0.0.1',0)); s.listen(); "
                             f"p=Path({str(path)!r}); t=p.with_suffix('.tmp'); "
                             "t.write_text(json.dumps([os.getpid(),s.getsockname()[1]])); t.replace(p); time.sleep(120)")
            program = ("import sys,time; from start_backend import Supervisor; owner=Supervisor(); "
                       f"owner.spawn('abrupt launcher fixture',[sys.executable,'-c',{child_program!r}]); time.sleep(120)")
            launcher = subprocess.Popen([sys.executable, '-c', program], cwd=start_backend.ROOT,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            owned = None
            try:
                deadline = time.monotonic() + 15
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(path.exists())
                pid, port = json.loads(path.read_text())
                owned = psutil.Process(pid)
                launcher.kill(); launcher.wait(timeout=5)
                deadline = time.monotonic() + 5
                while owned.is_running() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertFalse(owned.is_running())
                with socket.socket() as probe:
                    probe.settimeout(1)
                    self.assertNotEqual(0, probe.connect_ex(('127.0.0.1', port)))
            finally:
                if launcher.poll() is None:
                    launcher.kill(); launcher.wait(timeout=5)
                if owned is not None and owned.is_running():
                    owned.kill(); owned.wait(timeout=5)

    def test_mongo_close_runs_even_if_executor_close_fails(self):
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        app.repo = Mock()
        app.executor = Mock(close=Mock(side_effect=RuntimeError('worker failed')))
        with self.assertRaisesRegex(RuntimeError, 'worker failed'):
            app.close()
        app.repo.close.assert_called_once()

    def test_constructor_failure_releases_created_mongo_client(self):
        repo = Mock()
        with patch('postanalysis_api.services.application.MongoRepository', return_value=repo), patch(
                'postanalysis_api.services.application.SumoBatchExecutor', side_effect=RuntimeError('init failed')):
            with self.assertRaisesRegex(RuntimeError, 'init failed'):
                PostAnalysisApp('.')
        repo.close.assert_called_once()

    def test_executor_close_clears_frames_and_does_not_claim_a_live_worker_exited(self):
        executor = SumoBatchExecutor.__new__(SumoBatchExecutor)
        executor._lock = threading.RLock()
        executor._runtime = OrderedDict({'done': {'frame': {'persons': [object()]}}})
        executor._jobs, executor._pause_requests = {'job': threading.Event()}, {'job': threading.Event()}
        worker = Mock(name='stuck-worker')
        worker.name = 'stuck-worker'
        worker.is_alive.return_value = True
        executor._threads = {'job': worker}
        with self.assertRaisesRegex(RuntimeError, 'stuck-worker'):
            executor.close()
        self.assertTrue(executor._jobs['job'].is_set())
        self.assertTrue(executor._runtime)
        worker.is_alive.return_value = False
        executor.close()
        self.assertFalse(executor._runtime)
        self.assertFalse(executor._threads)
        self.assertFalse(executor._pause_requests)


class ListenerReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_setup_failure_still_closes_both_runtimes(self):
        app = Mock()
        server = OverlayServer.__new__(OverlayServer)
        server.create_app = lambda: web.Application()
        server._stop_runtime = AsyncMock()
        runner = Mock(setup=AsyncMock(side_effect=RuntimeError('setup failed')), cleanup=AsyncMock())
        with patch('crowdsim.infrastructure.websocket_server.PostAnalysisApp', return_value=app), patch(
                'crowdsim.infrastructure.websocket_server.web.AppRunner', return_value=runner):
            with self.assertRaisesRegex(RuntimeError, 'setup failed'):
                await server.start()
        runner.cleanup.assert_awaited_once()
        server._stop_runtime.assert_awaited_once()
        app.close.assert_called_once()

    async def test_failed_document_worker_still_closes_its_model_session(self):
        from data_service.api.knowledge_documents import register_document_routes
        from data_service.schemas.emergency_plans import PROFILE
        app = web.Application()
        model = Mock(open=AsyncMock(), close=AsyncMock())
        parser = Mock(run=AsyncMock(side_effect=RuntimeError('parser failed')))
        with patch('data_service.api.knowledge_documents.KnowledgeDocumentRepository'), patch(
                'data_service.api.knowledge_documents.DocumentParser', return_value=parser):
            register_document_routes(app, Mock(), PROFILE, model=model)
            lifecycle = app.cleanup_ctx[0](app)
            await anext(lifecycle)
            for _ in range(100):
                if parser.run.await_count:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(1, parser.run.await_count)
            with self.assertRaisesRegex(RuntimeError, 'parser failed'):
                await anext(lifecycle)
        model.close.assert_awaited_once()
