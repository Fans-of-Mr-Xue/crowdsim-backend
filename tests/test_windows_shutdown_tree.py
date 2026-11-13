"""Windows shutdown must include adopted SSH and every service descendant."""
import contextlib
import io
import os
import subprocess
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

if os.name == 'nt':
    import psutil
import start_backend


@unittest.skipUnless(os.name == 'nt', 'Windows process cleanup')
class WindowsShutdownTreeTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(patch('start_backend.WINDOWS', True))
        jobs = stack.enter_context(patch('start_backend.WindowsProcessJob'))
        jobs.return_value.pids.return_value = []

    def test_spawn_isolates_ctrl_c_but_keeps_ssh_password_input(self):
        child = Mock(pid=101)
        api = Mock(NoSuchProcess=psutil.NoSuchProcess)
        supervisor = start_backend.Supervisor()
        with patch('start_backend.subprocess.Popen', return_value=child) as popen, patch(
                'start_backend.importlib.import_module', return_value=api):
            supervisor.spawn('SSH', ['ssh', '-N'], interactive=True)
        self.assertEqual(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004, popen.call_args.kwargs['creationflags'])
        self.assertIsNone(popen.call_args.kwargs['stdin'])
        self.assertIs(supervisor.managed[101][1], api.Process.return_value)
        api.Process.return_value.resume.assert_called_once()

    def test_shutdown_snapshots_children_before_parents_and_escalates_survivors(self):
        events = []
        ssh = Mock(pid=101)
        service = Mock(pid=102)
        sumo = Mock(pid=103)
        ssh.children.side_effect = lambda **_: events.append('snapshot ssh') or []
        service.children.side_effect = lambda **_: events.append('snapshot service') or [sumo]
        ssh.terminate.side_effect = lambda: events.append('stop ssh')
        service.terminate.side_effect = lambda: events.append('stop service')
        sumo.terminate.side_effect = lambda: events.append('stop sumo')
        api = Mock(NoSuchProcess=psutil.NoSuchProcess, Error=psutil.Error)
        api.wait_procs.side_effect = [([ssh, service], [sumo]), ([sumo], [])]
        supervisor = start_backend.Supervisor()
        supervisor.managed = {101: ('SSH', ssh), 102: ('SUMO service', service)}
        with patch('start_backend.importlib.import_module', return_value=api):
            supervisor.close()
            supervisor.close()
        self.assertEqual(['snapshot ssh', 'snapshot service'], events[:2])
        self.assertLess(events.index('stop service'), events.index('stop ssh'))
        self.assertEqual({101, 102, 103}, {p.pid for p in api.wait_procs.call_args_list[0].args[0]})
        sumo.kill.assert_called_once()
        self.assertEqual({}, supervisor.managed)

    def test_reused_project_ssh_is_adopted_once_and_unrelated_ssh_is_untouched(self):
        supervisor = start_backend.Supervisor()
        identity = start_backend.ServiceProcess(101, 1, 'user', '1.0',
            ('ssh.exe', '-N', '-L', '127.0.0.1:27018:127.0.0.1:27018', '-L',
             '127.0.0.1:8800:127.0.0.1:8800', 'user@host'), start_backend.ROOT.resolve())
        with patch('start_backend.WindowsProcesses') as processes:
            api = processes.return_value
            api.owner = 'user'
            api.listening_pids.return_value = {101}
            api.identity.return_value = identity
            api.lifetime.return_value = ('user', '1.0')
            supervisor.manage_port(27018)
            supervisor.manage_port(8800)
            self.assertEqual([101], list(supervisor.managed))
            supervisor.managed.clear()
            for unrelated in (replace(identity, uid='other'), replace(identity, cwd=start_backend.ROOT.parent),
                              replace(identity, command=('ssh.exe', '-N', '-L', '127.0.0.1:9999:host:80', 'user@host'))):
                api.identity.return_value = unrelated
                supervisor.manage_port(27018)
                self.assertEqual({}, supervisor.managed)

    def test_reused_service_requires_matching_project_user_and_command(self):
        service = start_backend.Service('data', 8768, ['python.exe', '-m', 'data_service'], lambda: True)
        identity = start_backend.ServiceProcess(101, 1, 'user', '1.0', tuple(service.command), start_backend.ROOT.resolve())
        supervisor = start_backend.Supervisor()
        with patch('start_backend.WindowsProcesses') as processes:
            api = processes.return_value
            api.owner = 'user'
            api.listening_pids.return_value = {101}
            api.identity.return_value = identity
            api.lifetime.return_value = ('user', '1.0')
            supervisor.manage_port(8768, service)
            self.assertIn(101, supervisor.managed)
            supervisor.managed.clear()
            api.identity.return_value = replace(identity, command=('python.exe', '-m', 'unrelated'))
            with self.assertRaises(start_backend.StartupError):
                supervisor.manage_port(8768, service)
            self.assertEqual({}, supervisor.managed)

    def test_survivors_remain_managed_for_a_second_cleanup_attempt(self):
        service = Mock(pid=201)
        service.children.return_value = []
        api = Mock(NoSuchProcess=psutil.NoSuchProcess, Error=psutil.Error)
        api.wait_procs.side_effect = [([], [service])] * 4 + [([service], []), ([], [])]
        supervisor = start_backend.Supervisor()
        supervisor.managed = {201: ('service', service)}
        with patch('start_backend.importlib.import_module', return_value=api):
            with self.assertRaisesRegex(start_backend.StartupError, '退出清理未完成'):
                supervisor.close()
            self.assertIn(201, supervisor.managed)
            supervisor.close()
        self.assertFalse(supervisor.managed)
        self.assertEqual(3, service.terminate.call_count)
        self.assertEqual(2, service.kill.call_count)

    def test_optional_cleanup_failure_returns_ownership_to_launcher(self):
        supervisor = start_backend.Supervisor()
        child = Mock(pid=301)
        process, job = Mock(pid=301), Mock()
        supervisor.children = [('optional', child, False)]
        supervisor.managed = {301: ('optional', process)}
        supervisor.jobs = {301: job}
        cleanup = Mock(close=Mock(side_effect=RuntimeError('cleanup failed')))
        with patch('start_backend.Supervisor', return_value=cleanup):
            with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                supervisor._discard_children(list(supervisor.children))
        self.assertEqual([('optional', child, False)], supervisor.children)
        self.assertIs(job, supervisor.jobs[301])
        self.assertIs(process, supervisor.managed[301][1])


if __name__ == '__main__':
    unittest.main()
