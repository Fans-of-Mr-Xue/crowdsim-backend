import contextlib
import io
import os
import signal
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import start_backend


class SumoVersionTests(unittest.TestCase):
    def test_existing_and_legacy_version_output(self):
        cases = (
            ("Eclipse SUMO sumo 1.27.0\n Build features: Darwin arm64", "1.27.0"),
            ("Eclipse SUMO sumo 1.24.0\n", "1.24.0"),
            ("Eclipse SUMO sumo Version 1.24.0\n", "1.24.0"),
            ("Eclipse SUMO sumo Version v1_24_0+0-abcdef\n", "1.24.0"),
            ("Eclipse SUMO sumo Version 1.27.0+0-abcdef\n", "1.27.0"),
        )
        for output, expected in cases:
            with self.subTest(output=output), patch(
                "start_backend.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=output, stderr=""),
            ):
                self.assertEqual(expected, start_backend.check_sumo_version("sumo"))

    def test_unsupported_or_malformed_version_is_rejected(self):
        for output in ("sumo Version 1.23.0", "sumo 1.28.0", "sumo 1.24.01", "sumo 1.24.0rc1", "unknown"):
            with self.subTest(output=output), patch(
                "start_backend.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=output, stderr=""),
            ), self.assertRaises(start_backend.StartupError):
                start_backend.check_sumo_version("sumo")

    def test_broken_binary_is_rejected(self):
        with patch(
            "start_backend.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="Library not loaded"),
        ), self.assertRaisesRegex(start_backend.StartupError, "无法执行"):
            start_backend.check_sumo_version("sumo")


class DependencyTests(unittest.TestCase):
    def test_missing_data_service_dependencies_are_reported_together(self):
        def import_module(module):
            if module in ("pypdf", "bs4"):
                raise ModuleNotFoundError(f"No module named '{module}'")

        with patch("start_backend.importlib.import_module", side_effect=import_module):
            with self.assertRaises(start_backend.StartupError) as raised:
                start_backend.check_service_dependencies()
        message = str(raised.exception)
        self.assertIn("pypdf", message)
        self.assertIn("beautifulsoup4", message)
        self.assertIn(sys.executable, message)


class UnifiedStartupTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(patch("start_backend.check_service_dependencies"))
        stack.enter_context(patch("start_backend.check_sumo_version", return_value="1.27.0"))
        stack.enter_context(patch("crowdsim.infrastructure.sumo_adapter.discover_sumo_binary", return_value="sumo"))
        stack.enter_context(patch("start_backend.signal.signal"))
        stack.enter_context(patch("start_backend.socket.create_connection", side_effect=AssertionError("network forbidden")))
        stack.enter_context(patch("start_backend.subprocess.Popen", side_effect=AssertionError("process forbidden")))
        self.tunnels = stack.enter_context(patch("start_backend.ensure_tunnels"))
        self.restart = stack.enter_context(patch("start_backend.restart_local_services"))
        self.supervisor = stack.enter_context(patch("start_backend.Supervisor")).return_value

    def test_check_mode_never_starts_services_or_tunnels(self):
        for scenario in ("hotspot", "research", "east-nanjing-road"):
            with self.subTest(scenario=scenario):
                self.assertEqual(0, start_backend.main(["--check", "--scenario", scenario]))
        self.tunnels.assert_not_called()
        self.supervisor.start_service.assert_not_called()

    def test_normal_startup_includes_experiment_control_after_simulation(self):
        self.supervisor.check_children.side_effect = KeyboardInterrupt
        self.assertEqual(0, start_backend.main([]))
        self.tunnels.assert_called_once_with(self.supervisor, enabled=True)
        services = [call.args[0] for call in self.supervisor.start_service.call_args_list]
        self.assertEqual([8768, 8767, 8765, 8766], [service.port for service in services])
        self.assertIn("data_service", services[0].command)
        self.assertIn("dialogue_service.server", services[1].command)
        self.assertIn("crowdsim_overlay_server.py", services[2].command)
        self.assertEqual(
            [sys.executable, "-m", "crowdsim.experiments", "--host", "127.0.0.1", "--port", "8766",
             "--gateway-url", "ws://127.0.0.1:8765"],
            services[3].command,
        )
        self.supervisor.close.assert_called_once()
        self.restart.assert_not_called()

    def test_restart_is_explicit_and_runs_before_starting_local_services(self):
        self.supervisor.check_children.side_effect = KeyboardInterrupt
        sequence = []
        self.restart.side_effect = lambda services: sequence.append(('restart', [s.port for s in services]))
        self.supervisor.start_service.side_effect = lambda service: sequence.append(('start', service.port))
        self.assertEqual(0, start_backend.main(['--restart-services', '--no-tunnels']))
        self.tunnels.assert_called_once_with(self.supervisor, enabled=False)
        self.assertEqual(('restart', [8768, 8767, 8765, 8766]), sequence[0])
        self.assertEqual([('start', port) for port in [8768, 8767, 8765, 8766]], sequence[1:])
        for call in self.supervisor.start_service.call_args_list:
            self.assertNotIn('--restart-services', call.args[0].command)

    def test_check_with_restart_flag_never_restarts_anything(self):
        self.assertEqual(0, start_backend.main(['--restart-services', '--check']))
        self.restart.assert_not_called()
        self.tunnels.assert_not_called()
        self.supervisor.start_service.assert_not_called()

    @unittest.skipUnless(hasattr(signal, 'SIGHUP'), 'POSIX hangup')
    def test_terminal_hangup_uses_the_normal_cleanup_path(self):
        def register(signum, handler):
            if signum == signal.SIGHUP:
                handler(signum, None)
        with patch('start_backend.signal.signal', side_effect=register):
            self.assertEqual(0, start_backend.main([]))
        self.supervisor.close.assert_called_once()
        self.supervisor.start_service.assert_not_called()

    def test_experiment_control_probe_requires_ready_orchestrator(self):
        self.supervisor.check_children.side_effect = KeyboardInterrupt
        self.assertEqual(0, start_backend.main([]))
        control_service = self.supervisor.start_service.call_args_list[-1].args[0]
        for response, expected in (({"data": {"orchestrator": "ready"}}, True),
                                   ({"data": {"orchestrator": "starting"}}, False), ({}, False)):
            with self.subTest(response=response), patch("start_backend.get_json", return_value=response) as get_json:
                self.assertEqual(expected, control_service.probe())
                get_json.assert_called_once_with(8766, "/crowdSim/control/health")

    def test_monitoring_arguments_are_forwarded_only_to_simulation_service(self):
        self.supervisor.check_children.side_effect = KeyboardInterrupt
        self.assertEqual(0, start_backend.main(['--monitor-performance', '--performance-no-system', '--performance-interval', '10']))
        services = [call.args[0] for call in self.supervisor.start_service.call_args_list]
        self.assertNotIn('--monitor-performance', services[0].command)
        self.assertNotIn('--monitor-performance', services[1].command)
        self.assertNotIn('--monitor-performance', services[3].command)
        self.assertNotIn('--performance-no-system', services[3].command)
        self.assertNotIn('--performance-interval', services[3].command)
        self.assertIn('--monitor-performance', services[2].command)
        self.assertIn('--performance-no-system', services[2].command)
        self.assertEqual('10', services[2].command[services[2].command.index('--performance-interval') + 1])

    def test_normal_startup_does_not_add_monitoring_flag(self):
        self.supervisor.check_children.side_effect = KeyboardInterrupt
        self.assertEqual(0, start_backend.main([]))
        for call in self.supervisor.start_service.call_args_list:
            self.assertNotIn('--monitor-performance', call.args[0].command)

    def test_preflight_failure_never_opens_tunnels(self):
        with patch("start_backend.check_service_dependencies", side_effect=start_backend.StartupError("missing pypdf")):
            self.assertEqual(1, start_backend.main([]))
        self.tunnels.assert_not_called()
        self.supervisor.start_service.assert_not_called()
        self.supervisor.close.assert_called_once()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_interactive_ssh_keeps_terminal_and_stops_only_its_child(self):
        supervisor = start_backend.Supervisor()
        child = Mock(pid=12345)
        child.poll.return_value = None
        with patch("start_backend.subprocess.Popen", return_value=child) as popen, patch("start_backend.os.killpg") as killpg:
            supervisor.spawn("SSH", ["ssh"], interactive=True)
            self.assertFalse(popen.call_args.kwargs["start_new_session"])
            self.assertIsNone(popen.call_args.kwargs["stdin"])
            supervisor.close()
        killpg.assert_not_called()
        child.terminate.assert_called_once()
        child.kill.assert_not_called()
        self.assertEqual([], supervisor.children)

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_background_service_keeps_group_cleanup(self):
        supervisor = start_backend.Supervisor()
        child = Mock(pid=12345)
        with patch("start_backend.subprocess.Popen", return_value=child) as popen, patch("start_backend.os.killpg") as killpg:
            supervisor.spawn("service", ["python"])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(subprocess.DEVNULL, popen.call_args.kwargs["stdin"])
            supervisor.close()
        killpg.assert_called_once_with(child.pid, signal.SIGTERM)
        child.terminate.assert_not_called()

    def test_unresponsive_ssh_is_killed_without_signalling_parent_group(self):
        supervisor = start_backend.Supervisor()
        child = Mock(pid=12345)
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired(["ssh"], 5), 0]
        with patch("start_backend.subprocess.Popen", return_value=child), patch("start_backend.os.killpg") as killpg:
            supervisor.spawn("SSH", ["ssh"], interactive=True)
            supervisor.close()
        killpg.assert_not_called()
        child.terminate.assert_called_once()
        child.kill.assert_called_once()

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_unresponsive_service_group_is_killed(self):
        supervisor = start_backend.Supervisor()
        child = Mock(pid=12345)
        child.wait.side_effect = [subprocess.TimeoutExpired(["python"], 5), 0]
        with patch("start_backend.subprocess.Popen", return_value=child), patch("start_backend.os.killpg") as killpg:
            supervisor.spawn("service", ["python"])
            supervisor.close()
        self.assertEqual([signal.SIGTERM, signal.SIGKILL], [call.args[1] for call in killpg.call_args_list])
        child.kill.assert_not_called()

    def test_default_reuse_does_not_take_ownership_or_stop_an_existing_service(self):
        service = start_backend.Service('dialogue', 8767, ['python', '-m', 'dialogue_service.server'], Mock(return_value=True))
        supervisor = start_backend.Supervisor()
        with patch('start_backend.listening', return_value=True), patch('start_backend.subprocess.Popen') as popen, patch('start_backend.os.killpg') as killpg:
            supervisor.start_service(service)
            supervisor.close()
        self.assertEqual([], supervisor.children)
        popen.assert_not_called()
        killpg.assert_not_called()

    def test_restart_mode_refuses_to_reuse_a_new_port_occupant(self):
        service = start_backend.Service('dialogue', 8767, ['python', '-m', 'dialogue_service.server'], Mock(return_value=True))
        supervisor = start_backend.Supervisor(reuse_existing=False)
        with patch('start_backend.listening', return_value=True), patch('start_backend.subprocess.Popen') as popen:
            with self.assertRaisesRegex(start_backend.StartupError, '再次占用'):
                supervisor.start_service(service)
        popen.assert_not_called()
        service.probe.assert_not_called()


@unittest.skipIf(os.name == 'nt', 'POSIX restart mode')
class RestartServicesTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(patch('start_backend.shutil.which', return_value='/usr/bin/tool'))
        stack.enter_context(patch('start_backend.os.geteuid', return_value=501))
        stack.enter_context(patch('start_backend.listening', return_value=False))
        self.services = [
            start_backend.Service('data', 8768, ['python', '-m', 'data_service'], lambda: True),
            start_backend.Service('dialogue', 8767, ['python', '-m', 'dialogue_service.server'], lambda: True),
            start_backend.Service('simulation', 8765, ['python', 'crowdsim_overlay_server.py'], lambda: True),
            start_backend.Service('control', 8766, ['python', '-m', 'crowdsim.experiments'], lambda: True),
        ]
        self.processes = {s.port: start_backend.ServiceProcess(s.port, 1, 501, 'Thu Oct 15 17:29:01 2026',
                          tuple(s.command), start_backend.ROOT.resolve()) for s in self.services}
        self.query = stack.enter_context(patch('start_backend.process_query', side_effect=self.query_processes))
        self.identity = stack.enter_context(patch('start_backend.process_identity', side_effect=lambda pid: self.processes.get(pid)))
        self.lifetime = stack.enter_context(patch('start_backend.process_lifetime', side_effect=lambda pid:
            (process.uid, process.started) if (process := self.processes.get(pid)) else None))
        self.health = stack.enter_context(patch('start_backend.get_json', return_value={'data': {'activeRunId': None, 'queuedRuns': 0}}))
        self.kill = stack.enter_context(patch('start_backend.os.kill', side_effect=lambda pid, _: self.processes.pop(pid)))

    def query_processes(self, command):
        if command[0] == 'lsof':
            port = int(next(arg.split(':')[1] for arg in command if arg.startswith('-iTCP:')))
            return f'p{port}' if port in self.processes else ''
        if command == ['ps', '-axo', 'pid=,ppid=']:
            return ''
        raise AssertionError('unexpected process query')

    def test_stops_only_four_known_local_processes_in_dependency_order(self):
        start_backend.restart_local_services(self.services)
        self.assertEqual([8766, 8765, 8767, 8768], [c.args[0] for c in self.kill.call_args_list])
        self.assertTrue(all(c.args[1] == signal.SIGTERM for c in self.kill.call_args_list))
        self.assertEqual({}, self.processes)
        self.health.assert_called_once_with(8766, '/crowdSim/control/health')

    def test_preflight_rejects_other_projects_users_or_commands_before_any_signal(self):
        original = self.processes[8768]
        for changed in [replace(original, cwd=start_backend.ROOT.parent), replace(original, uid=999),
                        replace(original, command=('python', '-m', 'other_service')),
                        replace(original, command=('python', '-c', 'pass'))]:
            self.processes[8768] = changed
            with self.subTest(changed=changed), self.assertRaises(start_backend.StartupError):
                start_backend.restart_local_services(self.services)
            self.kill.assert_not_called()

    def test_running_or_queued_experiments_and_unknown_health_block_all_stops(self):
        for data in [{'activeRunId': 'running', 'queuedRuns': 0}, {'activeRunId': None, 'queuedRuns': 1}, {},
                     {'activeRunId': None, 'queuedRuns': False}]:
            self.health.return_value = {'data': data}
            with self.subTest(data=data), self.assertRaises(start_backend.StartupError):
                start_backend.restart_local_services(self.services)
            self.kill.assert_not_called()
        self.health.side_effect = TimeoutError()
        with self.assertRaisesRegex(start_backend.StartupError, '无法确认实验状态'):
            start_backend.restart_local_services(self.services)
        self.kill.assert_not_called()

    def test_paused_or_direct_simulation_children_also_block_restart(self):
        original = self.query_processes
        self.query.side_effect = lambda command: '9911 8765' if command[0] == 'ps' else original(command)
        with self.assertRaisesRegex(start_backend.StartupError, '暂停'):
            start_backend.restart_local_services(self.services)
        self.kill.assert_not_called()

    def test_another_live_launcher_must_be_closed_by_its_owner(self):
        self.processes[8768] = replace(self.processes[8768], parent_pid=1234)
        self.processes[1234] = start_backend.ServiceProcess(1234, 1, 501, 'earlier', ('python', str(start_backend.ROOT / 'start_backend.py')), start_backend.ROOT.resolve())
        with self.assertRaisesRegex(start_backend.StartupError, r'Ctrl\+C'):
            start_backend.restart_local_services(self.services)
        self.kill.assert_not_called()

    def test_unknown_listener_or_multiple_listeners_are_never_stopped(self):
        for output in ['', 'p123\np456']:
            self.query.side_effect = None
            self.query.return_value = output
            with patch('start_backend.listening', return_value=True), self.subTest(output=output), self.assertRaises(start_backend.StartupError):
                start_backend.restart_local_services(self.services)
            self.kill.assert_not_called()

    def test_restart_never_targets_model_database_or_arbitrary_ports(self):
        for port in (8800, 27018, 8080):
            service = replace(self.services[0], port=port)
            with self.subTest(port=port), self.assertRaisesRegex(start_backend.StartupError, '8765～8768'):
                start_backend.restart_local_services([service])
            self.kill.assert_not_called()

    def test_recycled_pid_is_rejected_before_any_process_is_stopped(self):
        counts = {}
        def identity(pid):
            counts[pid] = counts.get(pid, 0) + 1
            process = self.processes.get(pid)
            return replace(process, started='new-process') if pid == 8768 and counts[pid] > 1 else process
        self.identity.side_effect = identity
        with self.assertRaisesRegex(start_backend.StartupError, '进程已变化'):
            start_backend.restart_local_services(self.services)
        self.kill.assert_not_called()

    def test_slow_shutdown_is_reported_without_force_killing(self):
        self.kill.side_effect = None
        with patch('start_backend.time.monotonic', side_effect=[0, 6]):
            with self.assertRaisesRegex(start_backend.StartupError, '未退出'):
                start_backend.restart_local_services(self.services)
        self.kill.assert_called_once_with(8766, signal.SIGTERM)

    def test_exit_wait_does_not_read_identity_after_termination(self):
        stopped = set()
        def identity(pid):
            if pid in stopped:
                raise start_backend.StartupError('cwd is already gone')
            return self.processes.get(pid)
        def terminate(pid, signum):
            stopped.add(pid)
            self.processes.pop(pid)
        self.identity.side_effect = identity
        self.kill.side_effect = terminate
        start_backend.restart_local_services(self.services)
        self.assertEqual({8765, 8766, 8767, 8768}, stopped)

    def test_exit_wait_does_not_signal_a_reused_pid(self):
        self.kill.side_effect = None
        self.lifetime.return_value = (501, 'new-process')
        self.lifetime.side_effect = None
        start_backend.restart_local_services(self.services)
        self.assertEqual(4, self.kill.call_count)
        self.assertTrue(all(call.args[1] == signal.SIGTERM for call in self.kill.call_args_list))

    def test_missing_process_tools_do_not_fall_back_to_killing_by_port(self):
        with patch('start_backend.shutil.which', return_value=None):
            with self.assertRaisesRegex(start_backend.StartupError, 'ps 和 lsof'):
                start_backend.restart_local_services(self.services)
        self.kill.assert_not_called()

class ProcessIdentityTests(unittest.TestCase):
    def test_process_identity_parser(self):
        with patch('start_backend.process_query', side_effect=[
            '501 1 S Thu Oct 15 17:29:01 2026 python -m data_service',
            f'p123\nfcwd\nn{start_backend.ROOT}',
        ]):
            result = start_backend.process_identity(123)
        self.assertEqual(501, result.uid)
        self.assertEqual(1, result.parent_pid)
        self.assertEqual('Thu Oct 15 17:29:01 2026', result.started)
        self.assertEqual(('python', '-m', 'data_service'), result.command)
        self.assertEqual(start_backend.ROOT.resolve(), result.cwd)

    def test_exited_and_zombie_processes_are_not_signalled_again(self):
        for output in ('', '501 1 Z Thu Oct 15 17:29:01 2026 [python]'):
            with self.subTest(output=output), patch('start_backend.process_query', return_value=output):
                self.assertIsNone(start_backend.process_identity(123))

    def test_exit_between_ps_and_lsof_is_not_an_identity_error(self):
        for final_state in ('', '501 Z Thu Oct 15 17:29:01 2026'):
            with self.subTest(final_state=final_state), patch('start_backend.process_query', side_effect=[
                '501 1 S Thu Oct 15 17:29:01 2026 python -m data_service',
                '', final_state,
            ]):
                self.assertIsNone(start_backend.process_identity(123))

    def test_missing_cwd_of_a_live_process_still_blocks_identity_checks(self):
        with patch('start_backend.process_query', side_effect=[
            '501 1 S Thu Oct 15 17:29:01 2026 python -m data_service',
            '', '501 S Thu Oct 15 17:29:01 2026',
        ]), self.assertRaisesRegex(start_backend.StartupError, '身份'):
            start_backend.process_identity(123)


class ProcessLifetimeTests(unittest.TestCase):
    def test_live_process_uses_only_uid_and_start_time(self):
        with patch('start_backend.process_query', return_value='501 S Thu Oct 15 17:29:01 2026') as query:
            self.assertEqual((501, 'Thu Oct 15 17:29:01 2026'), start_backend.process_lifetime(123))
        query.assert_called_once_with(['ps', '-p', '123', '-o', 'uid=,stat=,lstart='])

    def test_exited_or_zombie_process_is_finished(self):
        for output in ('', '501 Z Thu Oct 15 17:29:01 2026'):
            with self.subTest(output=output), patch('start_backend.process_query', return_value=output):
                self.assertIsNone(start_backend.process_lifetime(123))

    def test_unreadable_state_is_not_treated_as_an_exited_process(self):
        with patch('start_backend.process_query', return_value='unreadable'), self.assertRaisesRegex(
            start_backend.StartupError, '退出状态'
        ):
            start_backend.process_lifetime(123)


if __name__ == "__main__":
    unittest.main()
