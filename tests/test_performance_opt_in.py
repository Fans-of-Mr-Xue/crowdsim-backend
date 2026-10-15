import contextlib
import ctypes
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
from crowdsim.infrastructure.performance_probe import PerformanceOptions, PerformanceProbe, performance_options, timed
from crowdsim.infrastructure.sumo_adapter import SumoAdapter
from crowdsim.infrastructure.decision_table_writer import DecisionTableWriter
from crowdsim.infrastructure.mac_performance_memory import NativeMemorySampler, RUsageInfoV2, SwapUsage, VMStatistics64
from crowdsim_overlay_server import build_requirement_runtime, build_runtime, parse_args
from scripts import collect_power_metrics, run_experiment, run_hotspot_experiment
import start_backend


class OptInTests(unittest.TestCase):
    def test_disabled_never_reads_clocks_creates_monitor_or_writes(self):
        forbidden = Mock(side_effect=AssertionError('diagnostic operation while disabled'))
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {
            'CROWDSIM_PERF': '1', 'CROWDSIM_SYSTEM_PERF': '1'
        }), patch('crowdsim.infrastructure.system_performance.SystemPerformanceMonitor', forbidden):
            probe = PerformanceProbe('disabled', clock=forbidden, cpu_clock=forbidden)
            runtime = SimpleNamespace(performance=probe)
            probe.attach(directory)
            probe.start_system(runtime)
            with probe.measure('unused'), probe.offline_cycle(runtime):
                pass
            @timed('unused')
            def work(instance):
                return 42
            self.assertEqual(42, work(runtime))
            probe.sample('unused', 3)
            probe.cycle(1, 1, 1)
            probe.flush(runtime, force=True)
            self.assertFalse(probe.frontend({'run_id': 'disabled'}))
            self.assertFalse(probe._append('performance.backend.jsonl', {}))
            probe.close_system()
            self.assertIsNone(probe.directory)
            self.assertIsNone(probe.system_monitor)
            self.assertEqual([], list(Path(directory).iterdir()))
            forbidden.assert_not_called()

    def test_cli_defaults_explicit_switch_and_invalid_combinations(self):
        for parser in (parse_args, run_experiment.parse_args, run_hotspot_experiment.parse_args):
            with self.subTest(parser=parser.__module__):
                self.assertFalse(performance_options(parser([])).enabled)
                options = performance_options(parser(['--monitor-performance', '--performance-no-system', '--performance-interval', '10']))
                self.assertEqual(PerformanceOptions(True, False, 10), options)
                for argv in (['--performance-no-system'], ['--performance-interval', '10'],
                             ['--monitor-performance', '--performance-interval', 'nan'],
                             ['--monitor-performance', '--performance-interval', '1']):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        parser(argv)

    def test_runtime_factory_and_reset_preserve_switch(self):
        with patch('crowdsim.infrastructure.sumo_adapter.discover_sumo_binary', return_value='unused'), patch.object(
            SimulationRuntime, 'initialize', return_value=None
        ):
            args = parse_args(['--monitor-performance', '--performance-no-system', '--performance-interval', '10'])
            runtime = build_runtime(args)
            self.assertEqual(args.performance_interval, runtime.performance.interval)
            self.assertTrue(runtime.performance.enabled)
            old = runtime.run_id
            runtime.reset()
            self.assertNotEqual(old, runtime.run_id)
            self.assertTrue(runtime.performance.enabled)
            self.assertFalse(runtime.performance.system_enabled)
            self.assertEqual(10, runtime.performance.interval)
            runtime.close()
            replacement = build_requirement_runtime(args, {'requirement': {'spatial_scope': {'location_id': 'east-nanjing-road'}}})
            self.assertEqual(runtime.performance_options, replacement.performance_options)
            replacement.close()
            normal = build_runtime(parse_args([]))
            with patch.object(normal.serializer, 'build_init', return_value={'type': 'init'}):
                self.assertFalse(normal.init_frame()['performance_measurement']['enabled'])
            normal.close()

    def test_write_failure_inside_measure_preserves_original_exception(self):
        probe = PerformanceProbe('failed', enabled=True)
        probe.attach('/nonexistent/performance/test')
        with self.assertRaisesRegex(RuntimeError, 'original error'), probe.measure('tick'):
            self.assertFalse(probe._append('performance.backend.jsonl', {}))
            raise RuntimeError('original error')
        self.assertFalse(probe.enabled)

    def test_thread_cpu_and_offline_speed_are_saved(self):
        clock, cpu = [0.0], [0.0]
        probe = PerformanceProbe('timed', enabled=True, clock=lambda: clock[0], cpu_clock=lambda: cpu[0])
        runtime = SimpleNamespace(time_seconds=0, state=RuntimeState.RUNNING, current=None, sim_speed_factor=3)
        with tempfile.TemporaryDirectory() as directory:
            probe.attach(directory)
            with probe.offline_cycle(runtime), probe.measure('sumo_snapshot'):
                runtime.time_seconds = .5
                clock[0], cpu[0] = .2, .05
            probe.flush(runtime, force=True)
            report = json.loads((Path(directory) / 'performance.backend.jsonl').read_text())
            self.assertAlmostEqual(2.5, report['actual_speed_factor'])
            self.assertAlmostEqual(200, report['stages']['sumo_snapshot']['mean'])
            self.assertAlmostEqual(50, report['stages']['sumo_snapshot']['cpu_mean'])

    def test_reused_service_must_match_monitor_configuration(self):
        normal, monitored = parse_args([]), parse_args(['--monitor-performance'])
        with patch('start_backend.get_json', return_value={'enabled': False, 'system_enabled': False, 'interval_wall_seconds': 5}):
            start_backend.check_performance_reuse(normal)
            with self.assertRaisesRegex(start_backend.StartupError, '监测参数'):
                start_backend.check_performance_reuse(monitored)
        with patch('start_backend.get_json', side_effect=OSError('old service')):
            with self.assertRaisesRegex(start_backend.StartupError, '重启'):
                start_backend.check_performance_reuse(normal)


class InstrumentationTests(unittest.TestCase):
    def adapter(self, probe):
        with patch('crowdsim.infrastructure.sumo_adapter.discover_sumo_binary', return_value='unused'):
            adapter = SumoAdapter('unused.sumocfg')
        adapter.performance = probe
        adapter.connection = SimpleNamespace(_sendCmd=Mock(return_value='answer'), close=Mock(),
            simulationStep=Mock(), simulation=SimpleNamespace(getDepartedPersonIDList=lambda: (), getArrivedPersonIDList=lambda: ()))
        return adapter

    def test_command_hook_is_local_transparent_and_restored(self):
        probe = PerformanceProbe('commands', enabled=True)
        adapter = self.adapter(probe)
        original = adapter.connection._sendCmd
        adapter._install_command_monitor()
        self.assertEqual('answer', adapter.connection._sendCmd(1, 'id', value=2))
        original.assert_called_once_with(1, 'id', value=2)
        original.side_effect = ValueError('traci failure')
        with self.assertRaisesRegex(ValueError, 'traci failure'):
            adapter.connection._sendCmd(2)
        self.assertEqual(2, probe.stats['traci_roundtrip']['count'])
        connection = adapter.connection
        with patch('traci.removeConnection', create=True):
            adapter.close()
        self.assertIs(original, connection._sendCmd)
        disabled = self.adapter(PerformanceProbe('normal'))
        original = disabled.connection._sendCmd
        disabled._install_command_monitor()
        self.assertIs(original, disabled.connection._sendCmd)

    def test_advance_and_snapshot_are_separate_without_extra_queries(self):
        probe = PerformanceProbe('step', enabled=True)
        adapter = self.adapter(probe)
        adapter.snapshot = Mock(return_value='frozen')
        self.assertEqual('frozen', adapter.step())
        adapter.connection.simulationStep.assert_called_once_with()
        adapter.snapshot.assert_called_once_with(departed=(), arrived=())
        self.assertEqual(1, probe.stats['sumo_advance']['count'])

    def test_business_decision_bytes_match_with_monitor_on_and_off(self):
        outputs = []
        with tempfile.TemporaryDirectory() as directory:
            for enabled in (False, True):
                path = Path(directory) / str(enabled)
                path.mkdir()
                writer = DecisionTableWriter(path, flush_every_records=1)
                writer.performance = PerformanceProbe('writer', enabled=enabled)
                writer.write_record({'context': None, 'candidates': [], 'plan': None, 'execution': {'status': 'applied'}})
                writer.close()
                outputs.append({file.name: file.read_bytes() for file in path.glob('*.jsonl')})
                self.assertEqual(enabled, 'decision_log_write' in writer.performance.stats)
                self.assertEqual(enabled, 'decision_log_flush' in writer.performance.stats)
        self.assertEqual(outputs[0], outputs[1])


class MemoryCounterTests(unittest.TestCase):
    def test_sdk_layout_sizes_and_counter_units(self):
        self.assertEqual(160, ctypes.sizeof(VMStatistics64))
        self.assertEqual(32, ctypes.sizeof(SwapUsage))
        self.assertEqual(160, ctypes.sizeof(RUsageInfoV2))
        self.assertEqual(144, VMStatistics64.total_uncompressed_pages_in_compressor.offset)
        self.assertEqual(152, VMStatistics64.swapped_count.offset)

    def test_memory_rates_warmup_reset_and_partial_sources(self):
        sampler = NativeMemorySampler()
        values = [VMStatistics64(swapins=value, compressions=value * 2, compressor_page_count=3) for value in (10, 20, 1)]
        values[0].available_words = 38
        def sysctl(key, struct_type):
            if key == 'kern.memorystatus_vm_pressure_level':
                raise PermissionError()
            return SwapUsage(used=100, total=200, available=100) if key == 'vm.swapusage' else ctypes.c_uint64(16 * 1024**3)
        with patch.object(sampler, '_vm', side_effect=values), patch.object(sampler, '_sysctl', side_effect=sysctl), patch(
            'crowdsim.infrastructure.mac_performance_memory.time.monotonic', side_effect=[0, 5, 10]
        ), patch('crowdsim.infrastructure.mac_performance_memory.os.sysconf', return_value=16384):
            first = sampler.memory()
            self.assertEqual('warming_up', first['page_rates']['status'])
            self.assertIsNone(first['swap_io_bytes_per_second']['swapins'])
            self.assertIsNone(first['memory_bytes']['swapped_logical'])
            second = sampler.memory()
            self.assertEqual('partial', second['status'])
            self.assertEqual(2 * 16384, second['swap_io_bytes_per_second']['swapins'])
            self.assertEqual(3 * 16384, second['memory_bytes']['compressor_physical'])
            self.assertEqual('PermissionError', second['pressure']['reason'])
            self.assertEqual('counter_reset', sampler.memory()['page_rates']['status'])

    def test_process_io_pid_reuse_and_failure_rebaseline(self):
        sampler = NativeMemorySampler()
        processes = {10: {'group': 'backend', 'identity': 'same'}, 20: {'group': 'browser_related_all_apps', 'identity': 'other'}}
        values = [RUsageInfoV2(proc_start_abstime=start, diskio_byteswritten=count) for start, count in ((1, 10), (1, 60), (2, 100), (2, 150))]
        with patch.object(sampler, '_usage', side_effect=[*values[:3], PermissionError(), values[3]]) as usage, patch(
            'crowdsim.infrastructure.mac_performance_memory.time.monotonic', side_effect=[0, 5, 10, 20]
        ):
            self.assertEqual('warming_up', sampler.process_io(processes)['processes'][0]['rates']['status'])
            self.assertEqual(10, sampler.process_io(processes)['processes'][0]['rates']['per_second']['disk_write_bytes'])
            self.assertEqual('pid_reused', sampler.process_io(processes)['processes'][0]['rates']['status'])
            self.assertEqual('unavailable', sampler.process_io(processes)['processes'][0]['status'])
            self.assertEqual('warming_up', sampler.process_io(processes)['processes'][0]['rates']['status'])
            self.assertEqual([10] * 5, [call.args[0] for call in usage.call_args_list])


class PowerCaptureTests(unittest.TestCase):
    def test_normal_run_and_non_admin_cannot_launch_power_capture(self):
        with tempfile.TemporaryDirectory() as directory, patch('scripts.collect_power_metrics.subprocess.run') as run:
            path = Path(directory) / 'manifest.json'
            path.write_text(json.dumps({'run_id': 'power', 'performance_measurement': {'enabled': False}}))
            args = collect_power_metrics.parse_args(['--run-dir', directory])
            with self.assertRaisesRegex(ValueError, '--monitor-performance'):
                collect_power_metrics.collect(args)
            path.write_text(json.dumps({'run_id': 'power', 'performance_measurement': {'enabled': True}}))
            with patch('scripts.collect_power_metrics.platform.system', return_value='Darwin'), patch(
                'scripts.collect_power_metrics.os.geteuid', return_value=501
            ), self.assertRaises(PermissionError):
                collect_power_metrics.collect(args)
            run.assert_not_called()
            self.assertEqual(['manifest.json'], [file.name for file in Path(directory).iterdir()])

    def test_explicit_power_capture_is_bounded_and_keeps_prior_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text(json.dumps({'run_id': 'power', 'performance_measurement': {'enabled': True}}))
            args = collect_power_metrics.parse_args(['--run-dir', directory, '--duration', '10'])
            with patch('scripts.collect_power_metrics.platform.system', return_value='Darwin'), patch(
                'scripts.collect_power_metrics.os.geteuid', return_value=0
            ), patch('scripts.collect_power_metrics.subprocess.run', return_value=SimpleNamespace(returncode=0, stderr='')) as run:
                result = collect_power_metrics.collect(args)
                self.assertEqual('complete', result['status'])
                self.assertEqual('power', result['run_id'])
                self.assertIn('cpu_power,gpu_power,thermal', run.call_args.args[0])
                self.assertEqual(40, run.call_args.kwargs['timeout'])
                with self.assertRaises(FileExistsError):
                    collect_power_metrics.collect(args)
                run.assert_called_once()
