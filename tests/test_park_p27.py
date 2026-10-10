"""P27's user-confirmed trapezoid geometry, retained bend and pedestrian routing."""

import itertools
import json
import math
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from scripts.add_park_p27 import (
    P17_TRISECTION, TWENTY_PERCENT, J21, SOUTHEAST_CORNER, NORTHEAST_CORNER,
    P27_A, P27_B, P27_C, P17_MIDDLE, P17_NORTH, P17_LAST, P26, P23,
    WALKING_AREA_WIDTHS, validate_applied,
)

ROOT = Path(__file__).resolve().parents[1]


def vector(a, b):
    return tuple(b[i] - a[i] for i in (0, 1))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class ParkP27Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "scenarios/shanghai_bund/bund.net.xml"
        cls.root = ET.parse(cls.path).getroot()
        cls.network = ResearchNetwork(str(cls.path))
        cls.router = PositionAwarePedestrianRouter(cls.network)
        cls.hotspot = next(h for h in json.loads((ROOT / "config/crowd_hotspots.json").read_text())["hotspots"]
                           if h["id"] == "people_heroes_monument")

    def coord(self, node):
        return self.network.net.getNode(node).getCoord()

    def test_chord_trapezoid_has_half_length_northeast_base_and_equal_legs(self):
        a, b, c, d = map(self.coord, (P17_TRISECTION, TWENTY_PERCENT, SOUTHEAST_CORNER, NORTHEAST_CORNER))
        long, short = vector(a, b), vector(c, d)
        for i in (0, 1): self.assertAlmostEqual(long[i] / 2, short[i], places=3)
        self.assertAlmostEqual(math.dist(a, c), math.dist(b, d), delta=0.001)
        unit = tuple(v / math.hypot(*long) for v in long)
        northeast = (unit[1], -unit[0])
        offset = vector(tuple((a[i]+b[i])/2 for i in (0,1)), tuple((c[i]+d[i])/2 for i in (0,1)))
        self.assertAlmostEqual(0, dot(offset, unit), delta=0.001)
        self.assertGreater(offset[0], 0); self.assertGreater(offset[1], 0)
        self.assertAlmostEqual(8.375878, dot(offset, northeast), delta=0.001)
        self.assertAlmostEqual(45.175423, math.dist(a, b), delta=0.001)
        self.assertAlmostEqual(22.587711, math.dist(c, d), delta=0.001)

    def test_southeast_leg_is_parallel_and_codirectional_with_p26(self):
        origin, a, c = map(self.coord, (J21, P17_TRISECTION, SOUTHEAST_CORNER))
        p26, leg = vector(origin, a), vector(a, c)
        sine = abs(p26[0]*leg[1] - p26[1]*leg[0]) / (math.hypot(*p26)*math.hypot(*leg))
        self.assertLess(sine, 0.00005)
        self.assertGreater(dot(p26, leg), 0)

    def test_three_segments_form_one_j34_to_j30_route_and_preserve_p17_bend(self):
        chain = ((P27_A, P17_TRISECTION, SOUTHEAST_CORNER),
                 (P27_B, SOUTHEAST_CORNER, NORTHEAST_CORNER),
                 (P27_C, NORTHEAST_CORNER, TWENTY_PERCENT))
        for key, start, end in chain:
            edge = self.root.find(f"edge[@id='{key}']")
            self.assertEqual((start, end), (edge.get("from"), edge.get("to")))
            lane = edge.find("lane")
            self.assertEqual("pedestrian", lane.get("allow"))
            self.assertEqual(2.0, float(lane.get("width")))
            self.assertEqual(2, len(lane.get("shape").split()))
        original = self.root.find(f"edge[@id='{P17_MIDDLE}']")
        points = [tuple(map(float, p.split(","))) for p in original.get("shape").split()]
        self.assertEqual([self.coord(TWENTY_PERCENT), (18570.17,5792.32), self.coord(P17_TRISECTION)], points)

    def test_four_junctions_have_actual_turn_connections_and_widening(self):
        validate_applied(self.root)
        for node, minimum in WALKING_AREA_WIDTHS.items():
            area = f":{node}_w0"
            lane = self.root.find(f"edge[@id='{area}']/lane")
            self.assertGreaterEqual(float(lane.get("width")), minimum)
            incident = [e for e in self.root.findall("edge") if node in (e.get("from"), e.get("to"))]
            self.assertEqual(2 if node in (SOUTHEAST_CORNER, NORTHEAST_CORNER) else 4, len(incident))
            for edge in incident:
                key = edge.get("id")
                source, target = (key, area) if edge.get("to") == node else (area, key)
                self.assertIsNotNone(self.root.find(f"connection[@from='{source}'][@to='{target}']"))
                self.assertIn(key, self.hotspot["park_access_edges"])
                self.assertIn(key, self.hotspot["visitor_spawn_edges"])

    def test_all_approaches_traverse_the_full_new_branch_in_both_directions(self):
        allowed = set(self.hotspot["visitor_spawn_edges"])
        forbidden = {e for e in self.router.walkable_edges if not e.startswith(":") and e not in allowed}
        for left, right, direction in itertools.product((P26, P17_LAST, P17_MIDDLE), (P23, P17_NORTH, P17_MIDDLE), (0,1)):
            with self.subTest(left=left, right=right, direction=direction):
                a = EdgePosition(left, self.network.edges[left].getLength()/2)
                b = EdgePosition(right, self.network.edges[right].getLength()/2)
                via = (P27_A,P27_B,P27_C)
                if direction: a,b,via = b,a,via[::-1]
                route = self.router.route_via_edges(a,b,via,forbidden_edges=forbidden,via_orientations=(direction,)*3)
                index = route.edges.index(via[0])
                self.assertEqual(via, tuple(route.edges[index:index+3]))
                self.assertTrue(set(route.edges).issubset(allowed))


if __name__ == "__main__":
    unittest.main()
