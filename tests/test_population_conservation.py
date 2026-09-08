from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from crowdsim.core.population_manager import PopulationManager
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
class PopulationConservationTests(unittest.TestCase):
    def test_flow_has_one_entity_per_person_and_no_replenishment(self):
        runtime = SimulationRuntime(
            SCENARIO / "scenario.sumocfg",
            pedestrian_route_files=[SCENARIO / "demand.rou.xml"],
        )
        try:
            runtime.initialize()
            runtime.start()
            for _ in range(300):
                runtime.tick()
                if runtime.state == RuntimeState.FINISHED:
                    break
            ledger = runtime.population.ledger
            self.assertEqual(40, len(ledger.departed_ids))
            self.assertEqual(40, len(ledger.arrived_ids))
            self.assertFalse(ledger.active_ids)
            self.assertFalse(ledger.unknown_disappearances)
            self.assertEqual(0, ledger.conservation_error)
        finally:
            runtime.close()

    def test_explicit_count_generates_exact_real_people_with_profile_types(self):
        bund = ROOT / "scenarios" / "shanghai_bund"
        manager = PopulationManager([bund / "bund_ped.rou.xml"])
        with tempfile.TemporaryDirectory() as directory:
            output = manager.prepare_demand(Path(directory) / "demand.rou.xml", 20)
            root = ET.parse(output).getroot()
            people = root.findall("person")
            types = {element.get("id"): float(element.get("maxSpeed")) for element in root.findall("vType")}
            self.assertEqual(20, len(people))
            self.assertEqual(20, len({person.get("id") for person in people}))
            self.assertTrue(all(person.get("type") in types for person in people))

    def test_configured_count_is_used_without_original_demand_duplication(self):
        bund = ROOT / "scenarios" / "shanghai_bund"
        runtime = SimulationRuntime(bund / "bund.research.sumocfg", pedestrian_route_files=[bund / "bund_ped.rou.xml"])
        runtime.configure_demand(20)
        try:
            runtime.initialize()
            runtime.start()
            for _ in range(4):
                runtime.tick()
            self.assertEqual(20, len(runtime.population.ledger.planned_ids))
            self.assertLessEqual(len(runtime.population.ledger.departed_ids), 20)
            self.assertNotIn("__rep", " ".join(runtime.population.ledger.departed_ids))
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
