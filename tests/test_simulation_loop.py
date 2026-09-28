from pathlib import Path
import json
import unittest
from unittest.mock import patch

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "tests" / "scenarios" / "unidirectional_corridor"


def has_sumo() -> bool:
    try:
        discover_sumo_binary()
        return True
    except FileNotFoundError:
        return False


@unittest.skipUnless(has_sumo(), "real SUMO binary is required")
class SimulationLoopTests(unittest.TestCase):
    def setUp(self):
        self.runtime = SimulationRuntime(SCENARIO / "scenario.sumocfg", pedestrian_route_files=[SCENARIO / "demand.rou.xml"])
        self.runtime.initialize()

    def tearDown(self):
        self.runtime.close()

    def test_tick_and_frame_share_one_time_boundary(self):
        self.runtime.start()
        result = self.runtime.tick()
        frame = self.runtime.frame()
        self.assertEqual(result.time_seconds, frame["step_seconds"])
        self.assertEqual(self.runtime.snapshot_id, frame["snapshot_id"])
        self.assertEqual("sumo", frame["metrics"]["pedestrian_engine"]["backend"])
        if frame["pedestrians"]:
            state = frame["pedestrians"][0]["state"]
            self.assertIn("nationality", state)
            self.assertIn("native_language", state)
            self.assertIn("decision_confidence", state)

    def test_pause_prevents_tick_and_resume_advances(self):
        self.runtime.start()
        self.runtime.tick()
        time_before_pause = self.runtime.time_seconds
        self.runtime.pause()
        with self.assertRaises(RuntimeError):
            self.runtime.tick()
        self.assertEqual(time_before_pause, self.runtime.time_seconds)
        self.runtime.start()
        self.runtime.tick()
        self.assertGreater(self.runtime.time_seconds, time_before_pause)

    def test_playback_speed_does_not_change_simulation_step(self):
        self.runtime.set_playback(speed_factor=4.0, push_fps=2.0)
        self.runtime.start()
        before = self.runtime.time_seconds
        self.runtime.tick()
        self.assertAlmostEqual(0.5, self.runtime.time_seconds - before)
        self.assertAlmostEqual(0.125, self.runtime.real_step_interval)

    def test_external_event_command_is_applied_at_next_tick_boundary(self):
        lon, lat = self.runtime.network.xy_to_lonlat(*self.runtime.center)
        queued = self.runtime.queue_command("set_event", {"id": "queued", "lng": lon, "lat": lat})
        self.assertEqual("queued", queued["status"])
        self.assertFalse(self.runtime.event_manager.events)
        self.runtime.start()
        self.runtime.tick()
        self.assertEqual("queued", self.runtime.event_manager.events[0].id)
        result = self.runtime.take_command_results()[0]
        self.assertEqual("applied", result["status"])
        self.assertEqual(0.0, result["applied_at"])

    def test_timeline_end_finishes_runtime_and_is_exposed_in_frame(self):
        self.runtime.timeline_end_seconds = 1.0
        init = self.runtime.init_frame()
        self.assertEqual("READY", init["runtime_state"])
        self.runtime.start()
        self.runtime.tick()
        self.runtime.tick()
        frame = self.runtime.frame()
        self.assertEqual(1.0, frame["step_seconds"])
        self.assertEqual("FINISHED", frame["runtime_state"])
        self.assertEqual("FINISHED", self.runtime.state.value)

    def test_close_flushes_decision_tables_and_summary(self):
        self.runtime.start()
        for _ in range(8):
            self.runtime.tick()
        recorder = self.runtime.recorder
        self.assertGreater(recorder.decision_writer.rows["decisions"], 0)
        self.runtime.close()
        self.assertTrue(all(handle.closed for handle in recorder.decision_writer.handles.values()))
        summary = json.loads((recorder.directory / "summary.json").read_text())
        self.assertEqual("CLOSED", summary["state"])
        self.assertTrue(summary["engine"]["closed"])
        self.assertIsNone(summary["engine"]["close_error"])
        self.assertEqual(summary, self.runtime.diagnostics())
        self.assertEqual("complete", summary["decision_log"]["status"])
        self.assertEqual(recorder.decision_writer.rows["decisions"], summary["decision_log"]["records"])

    def test_reset_closes_old_writer_and_starts_new_table_ids(self):
        self.runtime.start()
        self.runtime.tick()
        old = self.runtime.recorder
        self.runtime.reset()
        new = self.runtime.recorder
        self.assertNotEqual(old.directory, new.directory)
        self.assertTrue(old.decision_writer.closed)
        self.assertFalse(new.decision_writer.closed)
        self.assertEqual(0, sum(new.decision_writer.rows.values()))

    def test_tick_error_aborts_logger_without_masking_error(self):
        self.runtime.start()
        recorder = self.runtime.recorder
        with patch.object(self.runtime, "_prepare_boundary", side_effect=ValueError("forced tick error")):
            with self.assertRaisesRegex(ValueError, "forced tick error"):
                self.runtime.tick()
        self.assertEqual("ERROR", self.runtime.state.value)
        self.assertTrue(recorder.decision_writer.closed)
        self.assertTrue(self.runtime.adapter.diagnostics["closed"])
        manifest = json.loads((recorder.directory / "decision_manifest.json").read_text())
        self.assertEqual("aborted", manifest["status"])

    def test_initialization_error_closes_created_logger(self):
        runtime = SimulationRuntime(SCENARIO / "scenario.sumocfg", pedestrian_route_files=[SCENARIO / "demand.rou.xml"])
        try:
            with patch.object(ExperimentRecorder, "archive_demand", side_effect=ValueError("forced archive error")):
                with self.assertRaisesRegex(ValueError, "forced archive error"):
                    runtime.initialize()
            self.assertEqual("ERROR", runtime.state.value)
            self.assertTrue(runtime.recorder.decision_writer.closed)
            self.assertTrue(runtime.adapter.diagnostics["closed"])
            self.assertEqual("aborted", runtime.recorder.decision_writer.status)
        finally:
            runtime.close()

    def test_summary_write_failure_still_closes_logger_and_sumo(self):
        recorder = self.runtime.recorder
        with patch.object(recorder, "_write_json", side_effect=OSError("summary failure")):
            with self.assertRaisesRegex(OSError, "summary failure"):
                self.runtime.close()
        self.assertTrue(recorder.decision_writer.closed)
        self.assertTrue(self.runtime.adapter.diagnostics["closed"])


if __name__ == "__main__":
    unittest.main()
