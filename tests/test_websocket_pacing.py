import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crowdsim.core.simulation_runtime import RuntimeState
from crowdsim.infrastructure.performance_probe import PerformanceProbe
from crowdsim.infrastructure.websocket_server import OverlayServer


class WebsocketPacingTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, costs, *, speed=3, paused=False, change_speed=None, finish=False):
        clock = [0.0]
        sleeps, frames = [], []
        runtime = SimpleNamespace(state=RuntimeState.PAUSED if paused else RuntimeState.RUNNING,
                                  time_seconds=0.0, real_step_interval=.5 / speed)
        runtime.performance = PerformanceProbe('pacing', enabled=True, clock=lambda: clock[0])
        step = [0]

        async def tick():
            clock[0] += costs[step[0]][0]
            runtime.time_seconds += .5
            if finish:
                runtime.state = RuntimeState.FINISHED

        def frame():
            clock[0] += costs[step[0]][1]
            return {'type': 'update', 'step_seconds': runtime.time_seconds}

        async def send(payload):
            clock[0] += costs[step[0]][2]
            frames.append(json.loads(payload))
            if change_speed is not None:
                runtime.real_step_interval = .5 / change_speed

        runtime.tick_async = tick
        runtime.take_command_results = lambda: []
        runtime.frame = frame
        server = OverlayServer(runtime, '127.0.0.1', 0)
        server.client = SimpleNamespace(send=send)

        async def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
            step[0] += 1
            if step[0] >= len(costs):
                server.client = None

        with patch('crowdsim.infrastructure.websocket_server.time.perf_counter', side_effect=lambda: clock[0]), patch(
            'crowdsim.infrastructure.websocket_server.asyncio.sleep', side_effect=sleep
        ):
            await server._run_loop()
        return sleeps, frames, runtime.performance

    async def test_light_load_only_sleeps_remaining_budget_including_build_and_send(self):
        sleeps, frames, probe = await self.run_case([(.02, .01, .02)])
        self.assertAlmostEqual(1 / 6 - .05, sleeps[0])
        self.assertAlmostEqual(3, probe.simulated / probe.running_wall)
        self.assertEqual(.5, frames[0]['step_seconds'])
        self.assertAlmostEqual(1000 / 6, probe.stats['target_step_interval']['total'])
        self.assertAlmostEqual((1 / 6 - .05) * 1000, probe.stats['requested_sleep']['total'])

    async def test_over_budget_yields_zero_without_extra_delay(self):
        sleeps, frames, probe = await self.run_case([(.3, .05, .05)])
        self.assertEqual([0], sleeps)
        self.assertAlmostEqual(1.25, probe.simulated / probe.running_wall)
        self.assertEqual(1, len(frames))

    async def test_slow_step_does_not_cause_catch_up_burst(self):
        sleeps, frames, _ = await self.run_case([(.4, 0, 0), (.02, .01, .02)])
        self.assertEqual(0, sleeps[0])
        self.assertAlmostEqual(1 / 6 - .05, sleeps[1])
        self.assertEqual([.5, 1.0], [row['step_seconds'] for row in frames])

    async def test_latest_speed_is_used_for_remaining_budget(self):
        sleeps, _, _ = await self.run_case([(.02, .01, .02)], speed=1, change_speed=3)
        self.assertAlmostEqual(1 / 6 - .05, sleeps[0])
        sleeps, _, _ = await self.run_case([(.02, .01, .02)], speed=3, change_speed=1)
        self.assertAlmostEqual(.45, sleeps[0])

    async def test_paused_loop_waits_without_advancing_or_sending(self):
        sleeps, frames, probe = await self.run_case([(0, 0, 0)], paused=True)
        self.assertEqual([1 / 6], sleeps)
        self.assertEqual([], frames)
        self.assertEqual(0, probe.cycles)

    async def test_finished_step_has_no_extra_sleep(self):
        sleeps, frames, probe = await self.run_case([(.02, .01, .02)], finish=True)
        self.assertEqual([], sleeps)
        self.assertEqual(1, len(frames))
        self.assertAlmostEqual(.05, probe.running_wall)

    async def test_overloaded_loop_really_yields_for_pause_and_is_cancellable(self):
        runtime = SimpleNamespace(state=RuntimeState.RUNNING, time_seconds=0.0,
                                  real_step_interval=0.0, performance=PerformanceProbe('yield', enabled=False))
        async def tick():
            runtime.time_seconds += .5
        async def send(payload):
            pass
        runtime.tick_async = tick
        runtime.take_command_results = lambda: []
        runtime.frame = lambda: {'type': 'update'}
        server = OverlayServer(runtime, '127.0.0.1', 0)
        server.client = SimpleNamespace(send=send)
        task = asyncio.create_task(server._run_loop())
        try:
            await asyncio.sleep(0)
            self.assertGreater(runtime.time_seconds, 0)
            runtime.state = RuntimeState.PAUSED
            paused_time = runtime.time_seconds
            await asyncio.sleep(0)
            self.assertEqual(paused_time, runtime.time_seconds)
            runtime.state = RuntimeState.RUNNING
            await asyncio.sleep(0)
            self.assertGreater(runtime.time_seconds, paused_time)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
