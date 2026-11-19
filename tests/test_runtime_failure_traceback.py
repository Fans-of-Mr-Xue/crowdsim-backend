"""Failure-diagnostics persistence contract for SimulationRuntime.

Covers the traceback capture added after the archived v4 run lost its
``NoneType.get`` stack: on failure the runtime must keep both ``last_error``
and a structured ``last_traceback``, and a later state transition (e.g.
pause) must not overwrite or clear them. No real SUMO process is started;
the adapter connection is a mock, mirroring ``test_shutdown_diagnostics``.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime


SCENARIO = Path(__file__).parent / "scenarios" / "unidirectional_corridor"
THIS_FILE = os.path.basename(__file__)


class RuntimeFailureTracebackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        with patch("crowdsim.infrastructure.sumo_adapter.discover_sumo_binary", return_value="sumo"):
            self.runtime = SimulationRuntime(SCENARIO / "scenario.sumocfg")
        self.connection = Mock()
        self.connection.getVersion.return_value = (1, "test SUMO")
        self.runtime.adapter.connection = self.connection
        self.runtime.adapter.started = True
        self.runtime.adapter._version = "test SUMO"
        self.runtime.state = RuntimeState.READY
        # No recorder attached: cleanup skips the decision-log stage.
        self.runtime.recorder = None

    def tearDown(self):
        self.runtime.adapter.close()
        self.temporary.cleanup()

    def test_capture_failure_records_message_and_full_stack(self):
        try:
            raise ValueError("boom-none-get")
        except ValueError as exc:
            self.runtime._capture_failure(exc)

        self.assertEqual("ValueError: boom-none-get", self.runtime.last_error)
        trace = self.runtime.last_traceback
        self.assertIsNotNone(trace)
        self.assertIn("Traceback (most recent call last)", trace)
        self.assertIn("ValueError", trace)
        self.assertIn(THIS_FILE, trace)
        # Diagnostics project the captured stack verbatim.
        diagnostics = self.runtime.diagnostics()
        self.assertEqual(self.runtime.last_error, diagnostics["last_error"])
        self.assertIs(self.runtime.last_traceback, diagnostics["last_traceback"])
        # The persisted stack must not carry credential material.
        self.assertNotIn("api_key", trace.lower())

    def test_sync_tick_exception_persists_stack_and_enters_error_state(self):
        self.runtime.state = RuntimeState.RUNNING
        with patch.object(self.runtime, "_prepare_boundary", side_effect=RuntimeError("boom-none-get")):
            with self.assertRaisesRegex(RuntimeError, "boom-none-get"):
                self.runtime.tick()

        self.assertEqual(RuntimeState.ERROR, self.runtime.state)
        self.assertIn("RuntimeError: boom-none-get", self.runtime.last_error)
        self.assertIsNotNone(self.runtime.last_traceback)
        self.assertIn("RuntimeError", self.runtime.last_traceback)
        # The captured stack keeps a production-frame location (the mock raises
        # from inside unittest.mock, so the injection frame itself is not ours).
        self.assertIn("simulation_runtime.py", self.runtime.last_traceback)
        diagnostics = self.runtime.diagnostics()
        self.assertEqual(self.runtime.last_error, diagnostics["last_error"])
        self.assertIs(self.runtime.last_traceback, diagnostics["last_traceback"])

    def test_async_tick_exception_persists_traceback(self):
        self.runtime.state = RuntimeState.RUNNING
        with patch.object(self.runtime, "_prepare_boundary", side_effect=RuntimeError("async-boom")):
            with self.assertRaisesRegex(RuntimeError, "async-boom"):
                asyncio.run(self.runtime.tick_async())

        self.assertEqual(RuntimeState.ERROR, self.runtime.state)
        self.assertIn("RuntimeError: async-boom", self.runtime.last_error)
        self.assertIsNotNone(self.runtime.last_traceback)
        self.assertIn("async-boom", self.runtime.last_traceback)

    def test_pause_in_error_state_is_rejected_and_keeps_diagnostics(self):
        self.runtime.state = RuntimeState.ERROR
        self.runtime.last_error = "RuntimeError: boom"
        self.runtime.last_traceback = "STUB-STACK-TEXT"

        with self.assertRaisesRegex(RuntimeError, "pause is invalid"):
            self.runtime.pause()

        # The rejected state transition must not clear/overwrite the failure record.
        self.assertEqual("RuntimeError: boom", self.runtime.last_error)
        self.assertEqual("STUB-STACK-TEXT", self.runtime.last_traceback)
        diagnostics = self.runtime.diagnostics()
        self.assertEqual("RuntimeError: boom", diagnostics["last_error"])
        self.assertEqual("STUB-STACK-TEXT", diagnostics["last_traceback"])

    def test_failure_record_survives_resource_cleanup(self):
        self.runtime.state = RuntimeState.RUNNING
        with patch.object(self.runtime, "_prepare_boundary", side_effect=RuntimeError("cleanup-boom")):
            with self.assertRaisesRegex(RuntimeError, "cleanup-boom"):
                self.runtime.tick()

        # Abort cleanup must leave the original failure diagnostic in place.
        self.assertEqual(RuntimeState.ERROR, self.runtime.state)
        self.assertIn("RuntimeError: cleanup-boom", self.runtime.last_error)
        self.assertIsNotNone(self.runtime.last_traceback)


if __name__ == "__main__":
    unittest.main()
