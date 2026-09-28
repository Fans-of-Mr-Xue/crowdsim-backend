import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crowdsim.infrastructure.performance_probe import PerformanceProbe
from crowdsim.core.simulation_runtime import SimulationRuntime, RuntimeState
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
from crowdsim.infrastructure.websocket_server import OverlayServer
from tests.test_websocket_contract import has_sumo, CapturingClient

ROOT = Path(__file__).resolve().parents[1]


class PerformanceProbeTests(unittest.TestCase):
    def test_aggregate_and_actual_speed_exclude_idle_time(self):
        clock = [0.0]
        probe = PerformanceProbe('test', clock=lambda: clock[0], enabled=True)
        runtime = SimpleNamespace(time_seconds=1.0, state=RuntimeState.RUNNING, current=None, sim_speed_factor=3)
        with tempfile.TemporaryDirectory() as directory:
            probe.attach(directory)
            with probe.measure('tick_total'):
                clock[0] = 0.2
            probe.sample('tick_total', 400)
            probe.cycle(1.0, 2.0, .5)
            clock[0] = 6
            probe.flush(runtime)
            path = Path(directory) / 'performance.backend.jsonl'
            row = json.loads(path.read_text())
            self.assertEqual(300, row['stages']['tick_total']['mean'])
            self.assertEqual(.5, row['actual_speed_factor'])
            self.assertEqual(25, row['python_cpu_one_core_percent'])
            self.assertEqual(3, row['requested_speed_factor'])
            self.assertFalse(probe.stats)
            probe.flush(runtime, force=True)
            self.assertEqual(1, len(path.read_text().splitlines()))

    def test_frontend_scope_bounds_and_rate_limit(self):
        clock = [0.0]
        probe = PerformanceProbe('a', clock=lambda: clock[0], enabled=True)
        row = {'run_id': 'a', 'stages': {'canvas_draw': {'count': 2, 'total': 4, 'max': 3, 'mean': 2, 'unit': 'ms'}}, 'window_wall_seconds': 5}
        with tempfile.TemporaryDirectory() as directory:
            probe.attach(directory)
            self.assertFalse(probe.frontend({**row, 'run_id': 'b'}))
            self.assertFalse(probe.frontend({**row, 'active_people': float('nan')}))
            self.assertFalse(probe.frontend({**row, 'stages': {'../../other': {}}}))
            self.assertTrue(probe.frontend(row))
            self.assertFalse(probe.frontend(row))
            self.assertTrue(probe.frontend({**row, 'final': True}))
            self.assertFalse(probe.frontend({**row, 'final': True}))
            clock[0] += 5
            self.assertTrue(probe.frontend(row))
            self.assertEqual(3, len((Path(directory) / 'performance.frontend.jsonl').read_text().splitlines()))

    def test_disabled_and_failed_diagnostics_do_not_abort(self):
        disabled = PerformanceProbe('a', enabled=False)
        with disabled.measure('test'):
            pass
        self.assertFalse(disabled.stats)
        probe = PerformanceProbe('a', enabled=True)
        probe.attach('/nonexistent/performance/test')
        self.assertFalse(probe._append('performance.backend.jsonl', {}))
        self.assertFalse(probe.enabled)
        self.assertIsNotNone(probe.error)


@unittest.skipUnless(has_sumo(), 'real SUMO required')
class PerformanceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_on_off_preserves_simulation_results(self):
        scenario = ROOT / 'tests/scenarios/unidirectional_corridor'
        traces = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('crowdsim.core.simulation_runtime.PROJECT_ROOT', root), patch(
                'crowdsim.core.simulation_runtime.ExperimentRecorder',
                side_effect=lambda *args: ExperimentRecorder(*args, root=root / 'runs'),
            ):
                for enabled in (False, True):
                    runtime = SimulationRuntime(scenario / 'scenario.sumocfg', pedestrian_route_files=[scenario / 'demand.rou.xml'])
                    runtime.performance.enabled = enabled
                    trace = []
                    try:
                        runtime.initialize(); runtime.start()
                        for _ in range(12):
                            result = await runtime.tick_async()
                            trace.append([(pid, motion.x, motion.y, motion.speed, runtime.visual_states[pid],
                                           runtime.population.states[pid].stress)
                                          for pid, motion in sorted(result.persons.items())])
                        traces.append(trace)
                    finally:
                        runtime.close()
        self.assertEqual(traces[0], traces[1])

    async def test_loop_records_both_sides_without_extra_commands_or_changed_state(self):
        scenario = ROOT / 'tests/scenarios/unidirectional_corridor'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('crowdsim.core.simulation_runtime.PROJECT_ROOT', root), patch(
                'crowdsim.core.simulation_runtime.ExperimentRecorder',
                side_effect=lambda *args: ExperimentRecorder(*args, root=root / 'runs'),
            ):
                runtime = SimulationRuntime(scenario / 'scenario.sumocfg', pedestrian_route_files=[scenario / 'demand.rou.xml'])
                server = OverlayServer(runtime, '127.0.0.1', 0)
                client = CapturingClient(); server.client = client
                try:
                    await server._handle_message(json.dumps({'action': 'configure'}))
                    self.assertTrue(client.messages[-1]['performance_measurement']['enabled'])
                    await server._handle_message(json.dumps({'action': 'start'}))
                    await asyncio.sleep(1.2)
                    await server._handle_message(json.dumps({'action': 'pause'}))
                    await server._stop_loop()
                    runtime.performance.flush(runtime, force=True)
                    state, time_before = runtime.state, runtime.time_seconds
                    responses = len(client.messages)
                    await server._handle_message(json.dumps({'action': 'performance_report', 'report': {
                        'run_id': runtime.run_id, 'stages': {}, 'window_wall_seconds': 5,
                    }}))
                    self.assertEqual(responses, len(client.messages))
                    self.assertEqual(state, runtime.state)
                    self.assertEqual(time_before, runtime.time_seconds)
                    output = root / 'runs' / runtime.run_id
                    rows = [json.loads(line) for line in (output / 'performance.backend.jsonl').read_text().splitlines()]
                    stages = {key for row in rows for key in row['stages']}
                    self.assertTrue({'tick_total', 'sumo_step', 'neighbour_observation', 'state_update', 'record_step',
                                     'visual_classification', 'frame_build', 'json_encode', 'websocket_send', 'pacing_sleep'} <= stages)
                    self.assertTrue((output / 'performance.frontend.jsonl').is_file())
                    self.assertEqual(0, runtime.population.diagnostics()['conservation_error'])
                finally:
                    await server._stop_runtime()
