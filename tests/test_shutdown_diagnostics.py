import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder


SCENARIO = Path(__file__).parent / "scenarios" / "unidirectional_corridor"


class ShutdownDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        # Exercise the real runtime/recorder with a controllable TraCI close,
        # without starting a SUMO process for failure injection.
        with patch("crowdsim.infrastructure.sumo_adapter.discover_sumo_binary", return_value="sumo"):
            self.runtime = SimulationRuntime(SCENARIO / "scenario.sumocfg")
        self.connection = Mock()
        self.connection.getVersion.return_value = (1, "test SUMO")
        self.runtime.adapter.connection = self.connection
        self.runtime.adapter.started = True
        self.runtime.adapter._version = "test SUMO"
        self.runtime.state = RuntimeState.READY
        self.recorder = ExperimentRecorder("shutdown-test", SCENARIO / "scenario.sumocfg", {}, root=Path(self.temporary.name))
        self.runtime.recorder = self.recorder

    def tearDown(self):
        self.connection.close.side_effect = None
        self.runtime.adapter.close()
        self.recorder.decision_writer.close()
        self.temporary.cleanup()

    def summary(self):
        return json.loads((self.recorder.directory / "summary.json").read_text())

    def test_summary_observes_completed_resource_cleanup_and_close_is_idempotent(self):
        self.runtime.close()
        summary = self.summary()
        self.assertEqual("CLOSED", summary["state"])
        self.assertTrue(summary["engine"]["closed"])
        self.assertTrue(summary["decision_log"]["closed"])
        self.assertEqual("complete", summary["decision_log"]["status"])
        self.assertIsNone(summary["last_error"])
        self.assertEqual([], summary["close_errors"])
        self.assertEqual(summary, self.runtime.diagnostics())
        with patch.object(self.recorder, "write_summary", side_effect=AssertionError("duplicate finalization")):
            self.runtime.close()
        self.connection.close.assert_called_once_with(wait=True)

    def test_engine_close_failure_keeps_handle_closes_logs_and_can_retry(self):
        self.connection.close.side_effect = OSError("engine shutdown failed")
        with self.assertRaisesRegex(OSError, "engine shutdown failed"):
            self.runtime.close()
        summary = self.summary()
        self.assertEqual("ERROR", summary["state"])
        self.assertFalse(summary["engine"]["closed"])
        self.assertIn("engine shutdown failed", summary["engine"]["close_error"])
        self.assertTrue(summary["decision_log"]["closed"])
        self.assertEqual("aborted", summary["decision_log"]["status"])
        self.assertIs(self.connection, self.runtime.adapter.connection)
        self.connection.close.side_effect = None
        self.runtime.close()
        self.assertEqual("CLOSED", self.summary()["state"])
        self.assertTrue(self.summary()["engine"]["closed"])
        self.assertIn("engine shutdown failed", self.summary()["last_error"])
        self.assertIsNone(self.runtime.adapter.connection)

    def test_log_flush_failure_still_writes_error_summary_and_does_not_become_success_on_retry(self):
        with patch.object(self.recorder.decision_writer, "flush", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.runtime.close()
        summary = self.summary()
        self.assertEqual("ERROR", summary["state"])
        self.assertTrue(summary["engine"]["closed"])
        self.assertEqual("aborted", summary["decision_log"]["status"])
        self.assertIn("disk full", summary["decision_log"]["error"])
        self.assertTrue(all(handle.closed for handle in self.recorder.decision_writer.handles.values()))
        with self.assertRaisesRegex(OSError, "disk full"):
            self.runtime.close()
        self.assertEqual("ERROR", self.runtime.state.value)

    def test_multiple_cleanup_failures_keep_original_error_and_attempt_summary(self):
        self.runtime.last_error = "ValueError: original simulation failure"
        self.runtime.state = RuntimeState.ERROR
        self.connection.close.side_effect = OSError("engine failure")
        with patch.object(self.recorder.decision_writer, "flush", side_effect=OSError("log failure")):
            with self.assertRaisesRegex(OSError, "engine failure"):
                self.runtime.close()
        summary = self.summary()
        self.assertTrue(summary["last_error"].startswith("ValueError: original simulation failure"))
        self.assertEqual(["engine", "decision_log"], [item["stage"] for item in summary["close_errors"]])

    def test_summary_failure_releases_resources_and_retry_publishes_diagnostics(self):
        with patch.object(self.recorder, "_write_json", side_effect=OSError("summary failed")):
            with self.assertRaisesRegex(OSError, "summary failed"):
                self.runtime.close()
        self.assertTrue(self.runtime.adapter.closed)
        self.assertTrue(self.recorder.decision_writer.closed)
        self.assertEqual(RuntimeState.ERROR, self.runtime.state)
        self.assertFalse((self.recorder.directory / "summary.json").exists())
        self.runtime.close()
        self.assertEqual("CLOSED", self.summary()["state"])
        self.assertIn("summary failed", self.summary()["last_error"])
        self.assertFalse((self.recorder.directory / "summary.json.tmp").exists())

    def test_prior_simulation_error_remains_after_successful_resource_close(self):
        self.runtime.last_error = "ValueError: tick failed"
        self.runtime.state = RuntimeState.ERROR
        self.runtime._abort_resources()
        self.assertEqual(RuntimeState.ERROR, self.runtime.state)
        self.runtime.close()
        self.assertEqual("CLOSED", self.summary()["state"])
        self.assertEqual("ValueError: tick failed", self.summary()["last_error"])
        self.assertEqual("aborted", self.summary()["decision_log"]["status"])

    def test_startup_cleanup_does_not_claim_success_after_close_failure(self):
        self.connection.close.side_effect = OSError("startup cleanup failed")
        self.runtime.adapter._close_after_failure()
        self.assertFalse(self.runtime.adapter.closed)
        self.assertIs(self.connection, self.runtime.adapter.connection)
        self.assertIn("startup cleanup failed", self.runtime.adapter.close_error)

    def test_failed_log_handle_close_can_retry_without_claiming_success(self):
        writer = self.recorder.decision_writer
        handle = writer.handles["decisions"]
        with patch.object(handle, "close", side_effect=OSError("handle close failed")):
            with self.assertRaisesRegex(OSError, "handle close failed"):
                self.runtime.close()
        self.assertFalse(self.summary()["decision_log"]["closed"])
        self.assertFalse(handle.closed)
        with self.assertRaises(OSError):
            self.runtime.close()
        self.assertTrue(handle.closed)
        self.assertTrue(self.summary()["decision_log"]["closed"])
        self.assertEqual("ERROR", self.summary()["state"])


if __name__ == "__main__":
    unittest.main()
