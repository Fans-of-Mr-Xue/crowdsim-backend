"""Static M3 geometry, position routing and stored demand checks; no SUMO run."""

import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.infrastructure.network_adapter import ResearchNetwork


ROOT = Path(__file__).resolve().parents[1]
BUND = ROOT / "scenarios" / "shanghai_bund"


class MonumentM3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = ResearchNetwork(str(BUND / "bund.net.xml"))
        cls.router = PositionAwarePedestrianRouter(cls.network)
        cls.root = ET.parse(BUND / "bund.net.xml").getroot()
        cls.hotspot = next(
            item for item in json.loads((ROOT / "config" / "crowd_hotspots.json").read_text())["hotspots"]
            if item["id"] == "people_heroes_monument"
        )

    def test_m3_joins_j13_to_the_shorter_outer_arc(self):
        edge = self.root.find("edge[@id='monument_m3']")
        self.assertEqual("8417005112", edge.get("from"))
        self.assertEqual("monument_m3_ring_junction", edge.get("to"))
        neighbours = {
            other.get("id") for other in self.root.findall("edge")
            if other.get("from") == edge.get("to") or other.get("to") == edge.get("to")
        }
        self.assertEqual({"monument_m3", "679361567#2", "679361567#2_m3_north"}, neighbours)
        self.assertEqual("pedestrian", edge.find("lane").get("allow"))
        self.assertEqual(2.0, float(edge.find("lane").get("width")))
        self.assertGreater(float(edge.find("lane").get("length")), 0)
        short_length = sum(self.network.edges[e].getLength() for e in ("679361567#2", "679361567#2_m3_north"))
        self.assertGreater(self.network.edges["679361567#1"].getLength(), short_length)

    def test_every_park_path_and_viewing_zone_can_use_m3_in_both_directions(self):
        allowed = set(self.hotspot["park_access_edges"]) | set(self.hotspot["target_edges"]) | {"monument_m3"}
        forbidden = {
            identifier for identifier in self.router.walkable_edges
            if not identifier.startswith(":") and identifier not in allowed
        }
        for spawn in self.hotspot["park_access_edges"]:
            start = EdgePosition(spawn, self.network.edges[spawn].getLength() / 2)
            for zone in self.hotspot["viewing_zones"]:
                target = EdgePosition(zone["edge"], sum(zone["position_range_meters"]) / 2)
                with self.subTest(spawn=spawn, zone=zone["id"]):
                    for first, last, orientation in ((start, target, 0), (target, start, 1)):
                        route = self.router.route_via_edges(
                            first, last, ("monument_m3",),
                            forbidden_edges=forbidden, via_orientations=(orientation,),
                        )
                        self.assertEqual(1, route.edges.count("monument_m3"))
                        self.assertTrue(set(route.edges).issubset(allowed))
                        self.assertEqual(first.edge_id, route.edges[0])
                        self.assertEqual(last.edge_id, route.edges[-1])

    def test_stored_hotspot_demand_uses_all_three_portals_and_valid_positions(self):
        people = ET.parse(BUND / "bund_hotspot.rou.xml").getroot().findall("person")
        self.assertEqual(self.hotspot["visitor_count"], len(people))
        inbound_portals = set()
        outbound_portals = set()
        for person in people:
            walks = person.findall("walk")
            self.assertEqual(2, len(walks))
            for index, walk in enumerate(walks):
                identifiers = walk.get("edges").split()
                portals = set(identifiers) & set(self.hotspot["entry_edges"])
                expected_portals = 0 if index == 0 and identifiers[0] in self.hotspot["target_edges"] else 1
                self.assertEqual(expected_portals, len(portals))
                (inbound_portals if index == 0 else outbound_portals).update(portals)
                for identifier in identifiers:
                    self.assertIn(identifier, self.router.walkable_edges)
                for first, last in zip(identifiers, identifiers[1:]):
                    a, b = self.network.edges[first], self.network.edges[last]
                    self.assertTrue(
                        {a.getFromNode().getID(), a.getToNode().getID()}
                        & {b.getFromNode().getID(), b.getToNode().getID()},
                        f"disconnected stored walk: {first} -> {last}",
                    )
                self.assertGreaterEqual(float(walk.get("arrivalPos")), 0)
                self.assertLessEqual(float(walk.get("arrivalPos")), self.network.edges[identifiers[-1]].getLength())
            spawn = walks[0].get("edges").split()[0]
            self.assertGreaterEqual(float(person.get("departPos")), 0)
            self.assertLessEqual(float(person.get("departPos")), self.network.edges[spawn].getLength())
        self.assertEqual(set(self.hotspot["entry_edges"]), inbound_portals)
        self.assertEqual(set(self.hotspot["entry_edges"]), outbound_portals)


if __name__ == "__main__":
    unittest.main()
