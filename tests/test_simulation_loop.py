from pathlib import Path
import unittest

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary


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


if __name__ == "__main__":
    unittest.main()
