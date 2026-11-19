"""Server-boundary failure capture for the single-client WebSocket loop.

A failure raised outside the runtime's own main except (frame building, an
apply callback, or any outer ``await`` in ``_run_loop``) must still be captured
into the runtime diagnostics and emitted as a structured ``runtime_error``
frame carrying the full stack, instead of being reduced to ``Type: message``.
"""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from crowdsim.core.simulation_runtime import RuntimeState
from crowdsim.infrastructure.performance_probe import PerformanceProbe
from crowdsim.infrastructure.websocket_server import OverlayServer


def _runtime(extra=None):
    ns = SimpleNamespace(
        state=RuntimeState.RUNNING,
        time_seconds=0.0,
        run_id="loop-crash",
        performance=PerformanceProbe("boundary", enabled=False),
        take_command_results=lambda: [],
        last_error=None,
        last_traceback=None,
    )
    if extra:
        for key, value in extra.items():
            setattr(ns, key, value)
    return ns


class ServerBoundaryFailureTracebackTests(unittest.IsolatedAsyncioTestCase):
    async def _run_with(self, runtime, *, frame=None):
        frames = []

        async def send(payload):
            frames.append(json.loads(payload))

        server = OverlayServer(runtime, "127.0.0.1", 0)
        server.client = SimpleNamespace(send=send)
        if frame is not None:
            runtime.frame = frame
        await server._run_loop()
        return frames

    async def test_tick_exception_delegates_to_runtime_capture_and_emits_stack_frame(self):
        import traceback as _tb
        runtime = _runtime()

        def fake_capture(exc):
            runtime.last_error = f"{type(exc).__name__}: {exc}"
            runtime.last_traceback = "".join(
                _tb.format_exception(type(exc), exc, exc.__traceback__))

        runtime._capture_failure = Mock(side_effect=fake_capture)

        async def tick_async():
            raise AttributeError("'NoneType' object has no attribute 'get'")

        async def frame():
            return {"type": "update"}

        runtime.tick_async = tick_async
        with patch("crowdsim.infrastructure.websocket_server.logger") as logger:
            frames = await self._run_with(runtime, frame=frame)

        runtime._capture_failure.assert_called_once()
        self.assertEqual(RuntimeState.ERROR, runtime.state)
        self.assertIsNotNone(runtime.last_traceback)
        self.assertEqual(1, len(frames))
        frame0 = frames[0]
        self.assertEqual("runtime_error", frame0["type"])
        self.assertEqual("AttributeError", frame0["error_type"])
        self.assertIn("'NoneType' object has no attribute 'get'", frame0["message"])
        self.assertIn("Traceback", frame0["traceback"])
        self.assertIn("NoneType", frame0["traceback"])
        logger.exception.assert_called_once()

    async def test_frame_building_failure_outside_tick_is_captured_at_boundary(self):
        # The tick returns normally; the None.get is raised while building the
        # post-tick frame, which is outside the runtime main except.
        runtime = _runtime()
        captured = {}

        def fake_capture(exc):
            captured["type"] = type(exc).__name__
            captured["message"] = str(exc)
            import traceback as _tb
            captured["traceback"] = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
            runtime.last_error = f"{type(exc).__name__}: {exc}"
            runtime.last_traceback = captured["traceback"]

        runtime._capture_failure = fake_capture

        async def tick_async():
            runtime.time_seconds += 0.5

        def bad_frame():
            raise AttributeError("'NoneType' object has no attribute 'get'")

        runtime.tick_async = tick_async
        with patch("crowdsim.infrastructure.websocket_server.logger"):
            frames = await self._run_with(runtime, frame=bad_frame)

        self.assertEqual(RuntimeState.ERROR, runtime.state)
        self.assertEqual("AttributeError", captured["type"])
        self.assertIsNotNone(runtime.last_traceback)
        self.assertEqual(1, len(frames))
        self.assertEqual("runtime_error", frames[0]["type"])
        self.assertIn("NoneType", frames[0]["traceback"])

    async def test_defensive_capture_when_simulator_exposes_no_capture_method(self):
        # A plain namespace without _capture_failure/last_error must not raise
        # from the boundary; the structured frame still carries the stack.
        runtime = SimpleNamespace(
            state=RuntimeState.RUNNING,
            time_seconds=0.0,
            run_id="plain-namespace",
            performance=PerformanceProbe("plain", enabled=False),
            take_command_results=lambda: [],
        )

        async def tick_async():
            raise RuntimeError("boom")

        async def frame():
            return {"type": "update"}

        runtime.tick_async = tick_async
        with patch("crowdsim.infrastructure.websocket_server.logger"):
            frames = await self._run_with(runtime, frame=frame)

        self.assertEqual(1, len(frames))
        self.assertEqual("runtime_error", frames[0]["type"])
        self.assertIn("Traceback", frames[0]["traceback"])


if __name__ == "__main__":
    unittest.main()
