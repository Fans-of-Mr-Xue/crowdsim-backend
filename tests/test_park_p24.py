"""P24 equal-angle geometry, junction topology, bidirectional routing and demand."""

from collections import Counter
import itertools
import json
import math
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.scenarios.hotspot_demand import _destination_schedule
from scripts.add_park_p24 import JOIN, P22, P23, P24, P13_JUNCTION, P13_SOUTH, P13_NORTH, validate_applied

ROOT = Path(__file__).resolve().parents[1]


def vector(a, b):
    return tuple(b[i] - a[i] for i in (0, 1))


def angle(a, b):
    cosine = sum(x * y for x, y in zip(a, b)) / (math.hypot(*a) * math.hypot(*b))
    return math.degrees(math.acos(max(-1, min(1, cosine))))


class ParkP24Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ET.parse(ROOT / "scenarios/shanghai_bund/bund.net.xml").getroot()
        cls.network = ResearchNetwork(str(ROOT / "scenarios/shanghai_bund/bund.net.xml"))
        cls.router = PositionAwarePedestrianRouter(cls.network)
        cls.hotspot = next(h for h in json.loads((ROOT / "config/crowd_hotspots.json").read_text())["hotspots"]
                           if h["id"] == "people_heroes_monument")

    def coord(self, node):
        return self.network.net.getNode(node).getCoord()

    def test_straight_southwest_footway_has_equal_angles_at_j31_and_meets_p13(self):
        edge = self.root.find(f"edge[@id='{P24}']")
        c, end = map(self.coord, (JOIN, P13_JUNCTION))
        outgoing = vector(c, end)
        references = [vector(c, self.coord(self.root.find(f"edge[@id='{key}']").get("from"))) for key in (P22, P23)]
        angles = [angle(outgoing, direction) for direction in references]
        self.assertAlmostEqual(angles[0], angles[1], delta=0.001)
        self.assertAlmostEqual(135.8148, angles[0], places=3)
        self.assertLess(outgoing[0], 0); self.assertLess(outgoing[1], 0)
        self.assertAlmostEqual(34.2282, math.dist(c, end), places=3)
        lane = edge.find("lane")
        self.assertEqual("pedestrian", lane.get("allow"))
        self.assertEqual(2.0, float(lane.get("width")))
        self.assertEqual(2, len(lane.get("shape").split()))
        # Intersection is on P13's existing middle segment, not on its chord.
        a, b = (18540.68, 5738.70), (18528.97, 5788.05)
        u, v = vector(a, b), vector(a, end)
        self.assertLess(abs(u[0] * v[1] - u[1] * v[0]) / math.dist(a, b), 0.001)
        fraction = sum(x * y for x, y in zip(u, v)) / sum(x * x for x in u)
        self.assertGreater(fraction, 0); self.assertLess(fraction, 1)
        for key, bend in ((P13_SOUTH, a), (P13_NORTH, b)):
            shape = self.root.find(f"edge[@id='{key}']").get("shape")
            self.assertIn(bend, [tuple(map(float, p.split(","))) for p in shape.split()])

    def test_j31_and_j32_are_real_three_arm_widened_junctions(self):
        validate_applied(self.root)
        for node, neighbours in ((JOIN, {P22, P23, P24}), (P13_JUNCTION, {P13_SOUTH, P13_NORTH, P24})):
            area = f":{node}_w0"
            for key in neighbours:
                edge = self.root.find(f"edge[@id='{key}']")
                source, target = (key, area) if edge.get("to") == node else (area, key)
                self.assertIsNotNone(self.root.find(f"connection[@from='{source}'][@to='{target}']"))
                self.assertIn(key, self.hotspot["park_access_edges"])
                self.assertIn(key, self.hotspot["visitor_spawn_edges"])
        # A third-arm junction must have a polygon extending into P24.
        points = [tuple(map(float, p.split(","))) for p in self.root.find(f"edge[@id=':{JOIN}_w0']/lane").get("shape").split()]
        self.assertLess(min(y for _, y in points), self.coord(JOIN)[1] - 1)

    def test_both_p13_segments_can_use_p24_from_p22_and_p23_in_both_directions(self):
        allowed = set(self.hotspot["visitor_spawn_edges"])
        forbidden = {e for e in self.router.walkable_edges if not e.startswith(":") and e not in allowed}
        for branch, target in itertools.product((P22, P23), (P13_SOUTH, P13_NORTH)):
            start = EdgePosition(branch, self.network.edges[branch].getLength() / 2)
            end = EdgePosition(target, self.network.edges[target].getLength() / 2)
            for first, last, direction in ((start, end, 0), (end, start, 1)):
                with self.subTest(branch=branch, target=target, direction=direction):
                    route = self.router.route_via_edges(first, last, (P24,), forbidden_edges=forbidden, via_orientations=(direction,))
                    self.assertEqual(1, route.edges.count(P24))
                    self.assertTrue(set(route.edges).issubset(allowed))

    def test_p13_split_preserves_physical_road_destination_quota_and_valid_positions(self):
        schedule = _destination_schedule(self.network.net, self.hotspot, 1000, rng_seed=20260908)
        counts = Counter(key for key, _ in schedule)
        self.assertEqual(200, counts[P13_SOUTH] + counts[P13_NORTH])
        self.assertGreater(counts[P13_SOUTH], 0); self.assertGreater(counts[P13_NORTH], 0)
        self.assertEqual(200, counts["906417852#10"] + counts["906417852#10_p21_north"])
        for key in ("906417851#0", "906417852#7", "906417852#8"):
            self.assertEqual(200, counts[key])
        for key, position in schedule:
            self.assertGreaterEqual(position, 2)
            self.assertLessEqual(position, self.network.edges[key].getLength() - 2)

    def test_existing_forward_and_reverse_p13_walks_use_correct_segment_order(self):
        root = ET.parse(ROOT / "scenarios/shanghai_bund/bund_ped.rou.xml").getroot()
        for person, sequence in (("807", [P13_NORTH, P13_SOUTH]), ("1741", [P13_SOUTH, P13_NORTH])):
            ids = root.find(f"person[@id='{person}']/walk").get("edges").split()
            index = ids.index(sequence[0])
            self.assertEqual(sequence, ids[index:index + 2])


if __name__ == "__main__":
    unittest.main()
