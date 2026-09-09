from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from crowdsim.core.population_manager import PopulationManager
from crowdsim.scenarios.hotspot_demand import LOCK_PARAM, build_hotspot_demand


ROOT = Path(__file__).resolve().parents[1]
BUND = ROOT / "scenarios" / "shanghai_bund"


class HotspotDemandTests(unittest.TestCase):
    def test_builds_background_and_locked_multistage_hotspot_visitors(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "hotspot.rou.xml"
            report = build_hotspot_demand(
                BUND / "bund_ped.rou.xml",
                BUND / "bund.net.xml",
                ROOT / "config" / "crowd_hotspots.json",
                output,
                visitor_count=12,
                background_count=20,
            )
            people = ET.parse(output).getroot().findall("person")
            visitors = [person for person in people if person.get("id", "").startswith("hotspot.")]
            self.assertEqual(32, len(people))
            self.assertEqual(12, len(visitors))
            self.assertGreaterEqual(report["incoming_direction_count"], 2)
            self.assertTrue(all(len(person.findall("walk")) == 2 for person in visitors))
            self.assertTrue(all(len(person.findall("stop")) == 1 for person in visitors))
            self.assertTrue(all(any(param.get("key") == LOCK_PARAM for param in person.findall("param")) for person in visitors))
            manager = PopulationManager([output])
            self.assertEqual({person.get("id") for person in visitors}, manager.locked_itinerary_ids)


if __name__ == "__main__":
    unittest.main()
