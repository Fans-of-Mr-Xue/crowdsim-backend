import json
import os
import plistlib
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from crowdsim.infrastructure.system_performance import MacSampler, SystemPerformanceMonitor, command, parse_processes, process_group
from crowdsim.infrastructure.performance_probe import PerformanceProbe


PS = b'''100 1 1:02.50 2048 Mon Sep 28 10:00:00 2026 /opt/bin/python
101 100 0:03.00 1000 Mon Sep 28 10:00:01 2026 /opt/bin/sumo
102 1 0:08.00 1000 Mon Sep 28 10:00:01 2026 /other/sumo
103 1 0:10.00 5000 Mon Sep 28 10:00:01 2026 /Applications/Google Chrome
104 1 0:10.00 5000 Mon Sep 28 10:00:01 2026 /System/com.apple.WebKit.GPU
105 1 0:10.00 5000 Mon Sep 28 10:00:01 2026 /System/WindowServer
'''


class SystemSamplerTests(unittest.TestCase):
    def test_process_parse_and_attribution(self):
        rows, quality = parse_processes(PS)
        self.assertEqual({'raw_rows': 6, 'parsed_rows': 6, 'failed_rows': 0}, quality)
        self.assertEqual(62.5, rows[100]['cpu_seconds'])
        self.assertEqual(2048 * 1024, rows[100]['rss_bytes'])
        self.assertEqual('Google Chrome', rows[103]['name'])
        self.assertEqual('backend', process_group(100, rows[100], rows, 100))
        self.assertEqual('sumo_children', process_group(101, rows[101], rows, 100))
        self.assertIsNone(process_group(102, rows[102], rows, 100))
        self.assertEqual('browser_related_all_apps', process_group(104, rows[104], rows, 100))
        self.assertEqual('window_server', process_group(105, rows[105], rows, 100))
        self.assertEqual(({}, {'raw_rows': 1, 'parsed_rows': 0, 'failed_rows': 1}), parse_processes(b'invalid row'))

    def test_child_locale_is_fixed_without_changing_parent_environment(self):
        with patch.dict(os.environ, {'LC_ALL': 'zh_CN.UTF-8', 'LANG': 'zh_CN.UTF-8', 'CROWDSIM_TEST_MARKER': 'kept'}), patch(
            'crowdsim.infrastructure.system_performance.subprocess.run', return_value=SimpleNamespace(stdout=PS)
        ) as run:
            self.assertEqual(PS, command(['/bin/ps'], fixed_locale=True))
            self.assertEqual('C', run.call_args.kwargs['env']['LC_ALL'])
            self.assertEqual('C', run.call_args.kwargs['env']['LANG'])
            self.assertEqual('kept', run.call_args.kwargs['env']['CROWDSIM_TEST_MARKER'])
            self.assertEqual('zh_CN.UTF-8', os.environ['LC_ALL'])
            command(['/usr/sbin/ioreg'])
            self.assertIsNone(run.call_args.kwargs['env'])

    def test_localized_date_is_rejected_including_names_with_spaces(self):
        data = PS.decode().replace('Mon Sep 28', '一 9月/28').encode()
        rows, quality = parse_processes(data)
        self.assertEqual({}, rows)
        self.assertEqual(6, quality['failed_rows'])

    def test_long_cpu_time_and_bad_numeric_fields(self):
        for value, expected in [(b'123:02.50', 7382.5), (b'2:03:04.50', 7384.5), (b'1-02:03:04.50', 93784.5)]:
            rows, _ = parse_processes(PS.replace(b'1:02.50', value))
            self.assertEqual(expected, rows[100]['cpu_seconds'])
        for bad in (PS.replace(b'1:02.50', b'nan'), PS.replace(b'2048', b'-1'), PS.replace(b'Mon Sep 28', b'invalid date')):
            rows, quality = parse_processes(bad)
            self.assertNotIn(100, rows)
            self.assertGreater(quality['failed_rows'], 0)

    def test_missing_self_and_bad_rows_are_not_reported_ok(self):
        sampler = MacSampler()
        sampler.backend_pid = 100
        with patch('crowdsim.infrastructure.system_performance.command', return_value=PS + b'invalid row\n') as run:
            report = sampler.processes()
            self.assertEqual('partial', report['status'])
            self.assertEqual(1, report['quality']['failed_rows'])
            self.assertIsNone(report['groups']['backend']['rss_bytes_sum'])
            self.assertEqual(2048 * 1024, report['groups']['backend']['observed_rss_bytes_sum'])
            self.assertTrue(run.call_args.kwargs['fixed_locale'])
            self.assertIn('-ww', run.call_args.args[0])
        with patch('crowdsim.infrastructure.system_performance.command', return_value=PS.split(b'\n', 1)[1]):
            report = sampler.processes()
            self.assertEqual('unavailable', report['status'])
            self.assertFalse(report['quality']['backend_pid_found'])
            self.assertIsNone(report['groups']['browser_related_all_apps']['cpu_one_core_percent'])
            self.assertFalse(sampler.previous_processes)

    def test_zero_warmup_new_and_exited_processes_have_explicit_coverage(self):
        sampler = MacSampler()
        sampler.backend_pid = 100
        added = PS + b'106 1 0:02.00 5000 Mon Sep 28 10:00:02 2026 /System/com.apple.WebKit.WebContent\n'
        with patch('crowdsim.infrastructure.system_performance.command', side_effect=[PS, PS, added, added, PS, PS]), patch(
            'crowdsim.infrastructure.system_performance.time.monotonic', side_effect=[0, 5, 10, 15, 20, 25]
        ):
            first = sampler.processes()['groups']['browser_related_all_apps']
            self.assertEqual('warming_up', first['cpu_status'])
            self.assertIsNone(first['cpu_one_core_percent'])
            second = sampler.processes()['groups']['browser_related_all_apps']
            self.assertEqual('ok', second['cpu_status'])
            self.assertEqual(0, second['cpu_one_core_percent'])
            new = sampler.processes()['groups']['browser_related_all_apps']
            self.assertEqual((3, 2), (new['process_count'], new['cpu_measured_process_count']))
            self.assertEqual('partial', new['cpu_status'])
            self.assertIsNone(new['cpu_one_core_percent'])
            self.assertEqual(0, new['observed_cpu_one_core_percent'])
            self.assertEqual('ok', sampler.processes()['groups']['browser_related_all_apps']['cpu_status'])
            exited = sampler.processes()['groups']['browser_related_all_apps']
            self.assertEqual(1, exited['previous_processes_not_observed'])
            self.assertIsNone(exited['cpu_one_core_percent'])
            self.assertEqual('ok', sampler.processes()['groups']['browser_related_all_apps']['cpu_status'])

    def test_missing_optional_groups_do_not_mean_zero_or_sampling_failure(self):
        sampler = MacSampler()
        sampler.backend_pid = 100
        with patch('crowdsim.infrastructure.system_performance.command', return_value=PS.split(b'\n')[0]):
            report = sampler.processes()
        self.assertEqual('ok', report['status'])
        self.assertIn('sumo_children', report['quality']['missing_groups'])
        self.assertEqual('not_found', report['groups']['sumo_children']['status'])
        self.assertIsNone(report['groups']['sumo_children']['cpu_one_core_percent'])

    def test_command_failure_rebaselines_and_does_not_hide_other_sensors(self):
        sampler = MacSampler()
        sampler.backend_pid = 100
        with patch('crowdsim.infrastructure.system_performance.command', side_effect=[PS, TimeoutError(), PS]):
            sampler.processes()
            with self.assertRaises(TimeoutError):
                sampler.processes()
            self.assertEqual('warming_up', sampler.processes()['groups']['backend']['cpu_status'])

    def test_counter_reset_and_pid_reuse_do_not_produce_false_cpu(self):
        sampler = MacSampler()
        sampler.backend_pid = 100
        with patch('crowdsim.infrastructure.system_performance.command', side_effect=[PS, PS.replace(b'1:02.50', b'0:01.00'), PS.replace(b'10:00:00', b'11:00:00')]), patch(
            'crowdsim.infrastructure.system_performance.time.monotonic', side_effect=[0, 5, 10]
        ):
            sampler.processes()
            reset = sampler.processes()['groups']['backend']['processes'][0]
            self.assertEqual('counter_reset', reset['cpu_status'])
            self.assertIsNone(reset['cpu_one_core_percent'])
            reused = sampler.processes()['groups']['backend']
            self.assertEqual('pid_reused', reused['processes'][0]['cpu_status'])
            self.assertEqual(1, reused['previous_processes_not_observed'])

    def test_interval_cpu_and_pid_reuse(self):
        sampler = MacSampler()
        sampler.backend_pid = 100
        with patch('crowdsim.infrastructure.system_performance.command', side_effect=[PS, PS.replace(b'1:02.50', b'1:04.50'), PS.replace(b'10:00:00', b'11:00:00')]), patch(
            'crowdsim.infrastructure.system_performance.time.monotonic', side_effect=[0, 5, 10]
        ):
            self.assertIsNone(sampler.processes()['groups']['backend']['cpu_one_core_percent'])
            self.assertEqual(40, sampler.processes()['groups']['backend']['cpu_one_core_percent'])
            self.assertIsNone(sampler.processes()['groups']['backend']['cpu_one_core_percent'])
        self.assertNotIn(102, sampler.previous_processes)

    def test_gpu_and_battery_missing_is_not_zero(self):
        sampler = MacSampler()
        with patch('crowdsim.infrastructure.system_performance.command', return_value=plistlib.dumps([])):
            self.assertEqual('unavailable', sampler.gpu()['status'])
            self.assertIsNone(sampler.battery()['temperature_celsius'])
        with patch('crowdsim.infrastructure.system_performance.command', return_value=plistlib.dumps([
            {'PerformanceStatistics': {'Device Utilization %': 37, 'Renderer Utilization %': 500}, 'model': 'test'}
        ])):
            row = sampler.gpu()['devices'][0]
            self.assertEqual(37, row['device_utilization_percent'])
            self.assertIsNone(row['renderer_utilization_percent'])
        with patch('crowdsim.infrastructure.system_performance.command', return_value=plistlib.dumps([
            {'Temperature': 3070, 'IsCharging': False, 'ExternalConnected': True}
        ])):
            row = sampler.battery()
            self.assertEqual(30.7, row['temperature_celsius'])
            self.assertTrue(row['external_power_connected'])

    def test_independent_sensor_failures(self):
        sampler = MacSampler()
        for name in ('cpu', 'processes', 'gpu', 'thermal', 'battery'):
            setattr(sampler, name, Mock(side_effect=PermissionError('not allowed')))
        sampler.gpu = Mock(return_value={'status': 'ok'})
        result = sampler.sample()
        self.assertEqual('ok', result['gpu']['status'])
        self.assertEqual('PermissionError', result['cpu']['reason'])
        self.assertIsNone(result['chip_temperature']['cpu_celsius'])


class SystemMonitorTests(unittest.TestCase):
    def test_background_close_final_record_and_no_leaked_thread(self):
        ready = threading.Event()
        sampler = Mock()
        def sample():
            ready.set()
            return {'cpu': {'busy_percent': 20}}
        sampler.sample.side_effect = sample
        with tempfile.TemporaryDirectory() as directory:
            monitor = SystemPerformanceMonitor('test', directory, lambda: {'runtime_state': 'RUNNING', 'simulation_time': 5}, sampler=sampler)
            monitor.start()
            self.assertTrue(ready.wait(2))
            monitor.close()
            monitor.close()
            self.assertFalse(monitor.thread.is_alive())
            rows = [json.loads(line) for line in (Path(directory) / 'performance.system.jsonl').read_text().splitlines()]
            self.assertEqual(2, len(rows))
            self.assertTrue(rows[-1]['final'])
            self.assertEqual('test', rows[0]['run_id'])
            self.assertIsNone(monitor.error)

    def test_terminal_auto_stop_and_disk_failure_do_not_propagate(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = SystemPerformanceMonitor('test', directory, lambda: {'runtime_state': 'FINISHED'}, sampler=Mock(sample=lambda: {}))
            monitor.start()
            monitor.thread.join(2)
            self.assertFalse(monitor.thread.is_alive())
            self.assertTrue(json.loads((Path(directory) / 'performance.system.jsonl').read_text())['final'])
            broken = SystemPerformanceMonitor('bad', Path(directory) / 'missing', lambda: {}, sampler=Mock(sample=lambda: {}))
            broken.start()
            broken.thread.join(2)
            broken.close()
            self.assertIn('FileNotFoundError', broken.error)

    def test_disabled_and_non_mac(self):
        with tempfile.TemporaryDirectory() as directory:
            disabled = SystemPerformanceMonitor('test', directory, lambda: {}, enabled=False)
            disabled.start()
            disabled.close()
            self.assertIsNone(disabled.thread)
            with patch('crowdsim.infrastructure.system_performance.platform.system', return_value='Linux'):
                monitor = SystemPerformanceMonitor('test', directory, lambda: {})
                monitor.start()
                monitor.thread.join(2)
            self.assertFalse(monitor.thread.is_alive())
            self.assertEqual('unavailable', json.loads((Path(directory) / 'performance.system.jsonl').read_text())['host']['status'])

    def test_probe_switch_and_start_once(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'CROWDSIM_SYSTEM_PERF': '0'}):
            probe = PerformanceProbe('test', enabled=True)
            probe.attach(directory)
            runtime = SimpleNamespace(current=None, time_seconds=0, state=SimpleNamespace(value='READY'), sim_speed_factor=3)
            probe.start_system(runtime)
            first = probe.system_monitor
            probe.start_system(runtime)
            probe.close_system()
            self.assertIs(first, probe.system_monitor)
            self.assertFalse(probe.system_metadata()['enabled'])
            self.assertFalse(list(Path(directory).iterdir()))


if __name__ == '__main__':
    unittest.main()
