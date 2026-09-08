import asyncio
import os
import unittest

from crowdsim_overlay_server import OverlayNetworkSimulator, default_scenario_dir


class SimulationLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scenario = default_scenario_dir()
        cls.simulator = OverlayNetworkSimulator(
            os.path.join(scenario, "bund.net.xml"),
            os.path.join(scenario, "bund_ped.rou.xml"),
            os.path.join(scenario, "bund_veh.rou.xml"),
        )

    def test_agent_environment_decision_loop(self):
        simulator = self.simulator
        simulator.step()
        asyncio.run(simulator.resolve_agent_decisions())
        frame = simulator.frame()
        self.assertTrue(frame["pedestrians"])
        self.assertIn("density_level", frame["pedestrians"][0]["state"])
        self.assertEqual(
            len(frame["pedestrians"]),
            sum(frame["metrics"]["density_levels"].values()),
        )
        self.assertTrue(frame["metrics"]["pedestrian_engine"]["active"])

    def test_generic_event_is_exposed(self):
        simulator = self.simulator
        lat, lon = simulator.center
        simulator.set_event({
            "id": "test-event",
            "lng": lon,
            "lat": lat,
            "radius": 50,
            "intensity": 0.8,
            "densityMultiplier": 2.0,
            "duration": 30,
        })
        self.assertEqual("test-event", simulator.frame()["events"][-1]["id"])


if __name__ == "__main__":
    unittest.main()
