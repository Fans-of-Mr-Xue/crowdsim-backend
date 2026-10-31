"""P20 geometry, actual pedestrian turns and scoped network regeneration."""

import copy
import itertools
import json
import math
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from scripts.add_park_j05_j15 import EDGE_ID, merge_local, points


ROOT = Path(__file__).resolve().parents[1]
BUND = ROOT / "scenarios/shanghai_bund"


class ParkP20Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ET.parse(BUND / "bund.net.xml").getroot()
        cls.network = ResearchNetwork(str(BUND / "bund.net.xml"))
        cls.router = PositionAwarePedestrianRouter(cls.network)
        cls.hotspot = next(item for item in json.loads((ROOT / "config/crowd_hotspots.json").read_text())["hotspots"]
                           if item["id"] == "people_heroes_monument")

    def test_straight_bidirectional_footway_and_validated_endpoint_widths(self):
        north = self.root.find(f"edge[@id='{EDGE_ID}']")
        south = self.root.find("edge[@id='huangpu_park_j05_j15_p21_south']")
        last = self.root.find("edge[@id='huangpu_park_j05_j15_p21_south_p25_south']")
        self.assertEqual("1883306110", north.get("from"))
        self.assertEqual("8417005113", last.get("to"))
        self.assertEqual(north.get("to"), south.get("from"))
        self.assertEqual(south.get("to"), last.get("from"))
        a, b = (self.network.net.getNode(node).getCoord() for node in (north.get("from"), last.get("to")))
        for edge in (north, south, last):
            lane = edge.find("lane")
            self.assertEqual("pedestrian", lane.get("allow"))
            self.assertEqual(2.0, float(lane.get("width")))
            self.assertEqual(2, len(points(lane)))
            for x, y in points(lane):
                distance = abs((b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])) / math.dist(a, b)
                self.assertLess(distance, 0.01)
        for node, width, degree in (("1883306110", 4, 3), ("8417005113", 5, 4)):
            self.assertGreaterEqual(float(self.root.find(f"edge[@id=':{node}_w0']/lane").get("width")), width)
            self.assertEqual(degree, sum(node in (e.get("from"), e.get("to")) for e in self.root.findall("edge")))

    def test_all_approaches_can_use_p20_in_both_directions(self):
        allowed = set(self.hotspot["park_access_edges"]) | set(self.hotspot["entry_edges"]) | set(self.hotspot["target_edges"])
        self.assertIn(EDGE_ID, allowed)
        self.assertIn(EDGE_ID, self.hotspot["visitor_spawn_edges"])
        forbidden = {edge for edge in self.router.walkable_edges if not edge.startswith(":") and edge not in allowed}
        for north, south in itertools.product(("177931046", "906417852#0"), ("906417853#0", "906417853#1", "906417854")):
            start = EdgePosition(north, self.network.edges[north].getLength() / 2)
            target = EdgePosition(south, self.network.edges[south].getLength() / 2)
            for first, last, direction in ((start, target, 0), (target, start, 1)):
                with self.subTest(north=north, south=south, direction=direction):
                    via = (EDGE_ID, "huangpu_park_j05_j15_p21_south", "huangpu_park_j05_j15_p21_south_p25_south")
                    if direction:
                        via = via[::-1]
                    route = self.router.route_via_edges(first, last, via, forbidden_edges=forbidden, via_orientations=(direction,) * len(via))
                    self.assertEqual(1, route.edges.count(EDGE_ID))
                    self.assertEqual(1, route.edges.count("huangpu_park_j05_j15_p21_south"))
                    self.assertEqual(1, route.edges.count("huangpu_park_j05_j15_p21_south_p25_south"))
                    self.assertTrue(set(route.edges).issubset(allowed))
        near_j15 = "906417853#0_p22_east"
        route = self.router.route(EdgePosition("177931046", 3), EdgePosition(near_j15, self.network.edges[near_j15].getLength() - 1), forbidden_edges=forbidden)
        self.assertIn(EDGE_ID, route.edges)


class ScopedP20MergeTests(unittest.TestCase):
    def test_regeneration_preserves_unrelated_widening_and_far_end_geometry(self):
        base = '''<net version="1.20">
    <location netOffset="1,2" projParameter="!"/>
    <edge id="approach" from="outside_a" to="1883306110">
        <lane id="approach_0" index="0" allow="pedestrian" speed="1.39" length="10.00" width="2.00" shape="0,0 10,0"/>
    </edge>
    <edge id="exit" from="8417005113" to="outside_b">
        <lane id="exit_0" index="0" allow="pedestrian" speed="2.78" length="10.00" width="2.00" shape="20,0 30,0"/>
    </edge>
    <edge id=":1883306110_w0" function="walkingarea">
        <lane id=":1883306110_w0_0" width="4.00" length="2.00" shape="9,0 11,0"/>
    </edge>
    <edge id=":8417005113_w0" function="walkingarea">
        <lane id=":8417005113_w0_0" width="6.00" length="2.00" shape="19,0 21,0"/>
    </edge>
    <edge id=":unrelated_w0" function="walkingarea">
        <lane id=":unrelated_w0_0" width="7.00" length="2.00" shape="40,0 42,0"/>
    </edge>
    <junction id="1883306110" type="dead_end" x="10" y="0" shape="9,0 11,0"/>
    <junction id="8417005113" type="dead_end" x="20" y="0" shape="19,0 21,0"/>
    <junction id="unrelated" type="dead_end" x="40" y="0" shape="39,0 41,0"/>
    <connection from="approach" to=":1883306110_w0" fromLane="0" toLane="0"/>
    <connection from=":8417005113_w0" to="exit" fromLane="0" toLane="0"/>
    <connection from="untouched_a" to="untouched_b" fromLane="0" toLane="0"/>
</net>'''
        candidate = ET.fromstring(base)
        candidate.set("version", "1.27")
        candidate.find("location").set("netOffset", "100,200")
        candidate.find("edge[@id='approach']/lane").set("shape", "2,0 9,0")
        candidate.find("edge[@id='exit']/lane").set("shape", "21,0 28,0")
        for lane in candidate.findall("edge/lane"):
            lane.set("width", "2.00")
            lane.set("speed", "99")
        new = copy.deepcopy(candidate.find("edge[@id='exit']"))
        new.set("id", EDGE_ID)
        new.set("from", "1883306110")
        new.set("to", "8417005113")
        new.find("lane").set("id", EDGE_ID + "_0")
        candidate.append(new)
        ET.SubElement(candidate, "connection", {"from": ":1883306110_w0", "to": EDGE_ID, "fromLane": "0", "toLane": "0"})
        merged = ET.fromstring(merge_local(base, candidate))
        self.assertEqual("1.20", merged.get("version"))
        self.assertEqual("1,2", merged.find("location").get("netOffset"))
        self.assertEqual("7.00", merged.find("edge[@id=':unrelated_w0']/lane").get("width"))
        self.assertEqual("6.00", merged.find("edge[@id=':8417005113_w0']/lane").get("width"))
        for identifier, shape, speed in (("approach", "0.00,0.00 9.00,0.00", "1.39"), ("exit", "21.00,0.00 30.00,0.00", "2.78")):
            lane = merged.find(f"edge[@id='{identifier}']/lane")
            self.assertEqual(shape, lane.get("shape"))
            self.assertEqual("9.00", lane.get("length"))
            self.assertEqual(speed, lane.get("speed"))
        self.assertIsNotNone(merged.find(f"connection[@to='{EDGE_ID}']"))
        self.assertIsNotNone(merged.find("connection[@from='untouched_a']"))


if __name__ == "__main__":
    unittest.main()
