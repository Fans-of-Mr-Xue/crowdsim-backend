"""Check P21 geometry, real turning connections and split-road demand semantics."""

from collections import Counter
import copy
import itertools
import json
import math
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.scenarios.hotspot_demand import _destination_schedule
from scripts.add_park_p21 import EAST, WEST, P14_NORTH, P14_SOUTH, P20_NORTH, P20_SOUTH, P21


ROOT = Path(__file__).resolve().parents[1]


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


class ParkP21Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ET.parse(ROOT / "scenarios/shanghai_bund/bund.net.xml").getroot()
        cls.network = ResearchNetwork(str(ROOT / "scenarios/shanghai_bund/bund.net.xml"))
        cls.router = PositionAwarePedestrianRouter(cls.network)
        cls.hotspot = next(h for h in json.loads((ROOT / "config/crowd_hotspots.json").read_text())["hotspots"]
                           if h["id"] == "people_heroes_monument")

    def test_parallel_to_p15_at_40_percent_of_complete_north_to_south_p20(self):
        coord = lambda node: self.network.net.getNode(node).getCoord()
        north, south, east, west = map(coord, ("1883306110", "8417005113", EAST, WEST))
        whole = tuple(south[i] - north[i] for i in (0, 1))
        offset = tuple(east[i] - north[i] for i in (0, 1))
        self.assertAlmostEqual(0.4, sum(a * b for a, b in zip(whole, offset)) / sum(a * a for a in whole), places=6)
        self.assertLess(abs(cross(whole, offset)) / math.dist(north, south), 0.001)
        p15_start, p15_end = map(coord, ("8417005111", "8417005113"))
        p15 = tuple(p15_end[i] - p15_start[i] for i in (0, 1))
        p21 = tuple(east[i] - west[i] for i in (0, 1))
        self.assertLess(abs(cross(p15, p21)) / (math.dist(p15_start, p15_end) * math.dist(west, east)), 0.00001)
        self.assertLess(west[0], east[0])
        self.assertAlmostEqual(26.9104, math.dist(west, east), places=3)
        lane = self.root.find(f"edge[@id='{P21}']/lane")
        self.assertEqual("pedestrian", lane.get("allow"))
        self.assertEqual(2, len(lane.get("shape").split()))
        self.assertEqual(2.0, float(lane.get("width")))

    def test_split_roads_share_real_three_arm_widened_junctions(self):
        expected = {WEST: {P14_SOUTH, P14_NORTH, P21}, EAST: {P20_NORTH, P20_SOUTH, P21}}
        for node, neighbours in expected.items():
            with self.subTest(node=node):
                actual = {e.get("id") for e in self.root.findall("edge") if node in (e.get("from"), e.get("to"))}
                self.assertEqual(neighbours, actual)
                self.assertGreaterEqual(float(self.root.find(f"edge[@id=':{node}_w0']/lane").get("width")), 4.0)
                self.assertTrue(neighbours.issubset(self.hotspot["park_access_edges"]))
                self.assertTrue(neighbours.issubset(self.hotspot["visitor_spawn_edges"]))

    def test_p21_can_be_walked_from_each_split_segment_in_both_directions(self):
        allowed = set(self.hotspot["park_access_edges"]) | set(self.hotspot["entry_edges"]) | set(self.hotspot["target_edges"])
        forbidden = {e for e in self.router.walkable_edges if not e.startswith(":") and e not in allowed}
        for left, right in itertools.product((P14_SOUTH, P14_NORTH), (P20_NORTH, P20_SOUTH)):
            start = EdgePosition(left, self.network.edges[left].getLength() / 2)
            target = EdgePosition(right, self.network.edges[right].getLength() / 2)
            for first, last, direction in ((start, target, 0), (target, start, 1)):
                with self.subTest(left=left, right=right, direction=direction):
                    route = self.router.route_via_edges(first, last, (P21,), forbidden_edges=forbidden, via_orientations=(direction,))
                    self.assertEqual(1, route.edges.count(P21))
                    self.assertTrue(set(route.edges).issubset(allowed))

    def test_split_p14_keeps_its_original_one_fifth_destination_quota(self):
        schedule = _destination_schedule(self.network.net, self.hotspot, 1000, rng_seed=20260908)
        counts = Counter(edge for edge, _ in schedule)
        self.assertEqual(200, counts[P14_SOUTH] + counts[P14_NORTH])
        self.assertGreater(counts[P14_SOUTH], 0)
        self.assertGreater(counts[P14_NORTH], 0)
        self.assertEqual(200, counts["906417852#9"] + counts["906417852#9_p24_north"])
        for edge in ("906417851#0", "906417852#7", "906417852#8"):
            self.assertEqual(200, counts[edge])
        for edge, position in schedule:
            self.assertGreaterEqual(position, 2)
            self.assertLessEqual(position, self.network.edges[edge].getLength() - 2)

    def test_invalid_split_destination_weights_are_rejected(self):
        for weights in ({P14_SOUTH: 1}, {e: float("nan") for e in self.hotspot["visitor_destination_edges"]}):
            hotspot = copy.deepcopy(self.hotspot)
            hotspot["destination_edge_weights"] = weights
            with self.subTest(weights=weights), self.assertRaisesRegex(ValueError, "destination_edge_weights"):
                _destination_schedule(self.network.net, hotspot, 10, rng_seed=1)


if __name__ == "__main__":
    unittest.main()
