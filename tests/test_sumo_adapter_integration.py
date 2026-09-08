import os
from pathlib import Path
import unittest

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
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
class SumoAdapterIntegrationTests(unittest.TestCase):
    def make_runtime(self) -> SimulationRuntime:
        return SimulationRuntime(
            SCENARIO / "scenario.sumocfg",
            pedestrian_route_files=[SCENARIO / "demand.rou.xml"],
        )

    def test_real_person_departs_and_moves_without_python_motion(self):
        runtime = self.make_runtime()
        try:
            runtime.initialize()
            runtime.start()
            first_positions = {}
            moved = set()
            for _ in range(30):
                step = runtime.tick()
                for person_id, motion in step.persons.items():
                    first_positions.setdefault(person_id, (motion.x, motion.y))
                    if first_positions[person_id] != (motion.x, motion.y):
                        moved.add(person_id)
            self.assertTrue(runtime.population.ledger.departed_ids)
            self.assertTrue(moved)
            self.assertEqual(0, runtime.population.ledger.conservation_error)
            self.assertEqual("sumo", runtime.adapter.diagnostics["backend"])
        finally:
            runtime.close()
        self.assertEqual(RuntimeState.CLOSED, runtime.state)
        self.assertTrue(runtime.adapter.closed)

    def test_invalid_config_enters_error_and_cleans_up(self):
        runtime = SimulationRuntime(SCENARIO / "missing.sumocfg")
        with self.assertRaises(FileNotFoundError):
            runtime.initialize()
        self.assertEqual(RuntimeState.ERROR, runtime.state)
        self.assertTrue(runtime.adapter.closed)

    def test_bund_projection_round_trip_and_internal_edges_preserve_identity(self):
        bund = ROOT / "scenarios" / "shanghai_bund"
        runtime = SimulationRuntime(bund / "bund.research.sumocfg", pedestrian_route_files=[bund / "bund_ped.rou.xml"])
        seen_internal = None
        try:
            runtime.initialize()
            runtime.start()
            for _ in range(200):
                step = runtime.tick()
                for motion in step.persons.values():
                    x, y = runtime.network.lonlat_to_xy(motion.lon, motion.lat)
                    self.assertLessEqual(((x - motion.x) ** 2 + (y - motion.y) ** 2) ** 0.5, 0.1)
                    if motion.edge_id.startswith(":"):
                        seen_internal = motion.person_id
                        self.assertIn(motion.person_id, step.persons)
                if seen_internal:
                    break
            self.assertIsNotNone(seen_internal, "no real person reached an internal/walking-area edge")
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
