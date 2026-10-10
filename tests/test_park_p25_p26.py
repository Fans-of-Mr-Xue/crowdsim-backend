"""Full-centerline access fractions, five-arm turns and both new footway routes."""

import itertools
import json
import math
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from scripts.add_park_p25_p26 import (
    EAST, MIDPOINT, TWENTY_PERCENT, J21, P20_MIDPOINT, P17_TRISECTION,
    P20_SOUTH, P20_LAST, P17_MIDDLE, P17_LAST, P25, P26,
    WALKING_AREA_WIDTHS, validate_applied,
)

ROOT = Path(__file__).resolve().parents[1]


class ParkP25P26Tests(unittest.TestCase):
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

    def test_j33_is_exact_j28_j15_midpoint(self):
        a, b, middle = map(self.coord, (EAST, "8417005113", P20_MIDPOINT))
        self.assertLess(math.dist(middle, tuple((a[i] + b[i]) / 2 for i in (0, 1))), 0.001)
        self.assertAlmostEqual(math.dist(a, middle), math.dist(middle, b), places=3)
        for key, endpoints in ((P20_SOUTH, (EAST, P20_MIDPOINT)), (P20_LAST, (P20_MIDPOINT, "8417005113"))):
            edge = self.root.find(f"edge[@id='{key}']")
            self.assertEqual(endpoints, (edge.get("from"), edge.get("to")))

    def test_j34_is_near_j18_trisection_by_polyline_length_and_keeps_bend(self):
        j18, j30, middle = map(self.coord, ("8417005115", TWENTY_PERCENT, P17_TRISECTION))
        bend = (18570.17, 5792.32)
        full = math.dist(j18, bend) + math.dist(bend, j30)
        self.assertAlmostEqual(1 / 3, math.dist(j18, middle) / full, places=5)
        # Chord interpolation would move this access point off the original road.
        chord = tuple(j18[i] + (j30[i] - j18[i]) / 3 for i in (0, 1))
        self.assertGreater(math.dist(chord, middle), 0.5)
        shape = self.root.find(f"edge[@id='{P17_MIDDLE}']").get("shape").split()
        self.assertIn(bend, [tuple(map(float, p.split(","))) for p in shape])
        self.assertEqual(P17_TRISECTION, self.root.find(f"edge[@id='{P17_MIDDLE}']").get("to"))
        self.assertEqual((P17_TRISECTION, "8417005115"), tuple(self.root.find(f"edge[@id='{P17_LAST}']").get(k) for k in ("from", "to")))

    def test_both_new_roads_are_straight_two_meter_footways(self):
        for key, length in ((P25, 33.1958), (P26, 58.7053)):
            edge = self.root.find(f"edge[@id='{key}']")
            a, b = map(self.coord, (edge.get("from"), edge.get("to")))
            self.assertAlmostEqual(length, math.dist(a, b), places=3)
            lane = edge.find("lane")
            self.assertEqual("pedestrian", lane.get("allow"))
            self.assertEqual(2.0, float(lane.get("width")))
            points = [tuple(map(float, p.split(","))) for p in lane.get("shape").split()]
            self.assertEqual(2, len(points))
            for x, y in points:
                self.assertLess(abs((b[0]-a[0])*(y-a[1])-(b[1]-a[1])*(x-a[0])) / math.dist(a,b), 0.001)

    def test_four_affected_junctions_have_real_connections_and_widening(self):
        validate_applied(self.root)
        for node, width in WALKING_AREA_WIDTHS.items():
            area = f":{node}_w0"
            self.assertGreaterEqual(float(self.root.find(f"edge[@id='{area}']/lane").get("width")), width)
            incident = [e for e in self.root.findall("edge") if node in (e.get("from"), e.get("to"))]
            self.assertEqual(5 if node == J21 else 4, len(incident))
            for edge in incident:
                key = edge.get("id")
                source, target = (key, area) if edge.get("to") == node else (area, key)
                self.assertIsNotNone(self.root.find(f"connection[@from='{source}'][@to='{target}']"))
                self.assertIn(key, self.hotspot["park_access_edges"])
                self.assertIn(key, self.hotspot["visitor_spawn_edges"])

    def test_all_approaches_can_traverse_each_new_road_in_both_directions(self):
        allowed = set(self.hotspot["visitor_spawn_edges"])
        forbidden = {e for e in self.router.walkable_edges if not e.startswith(":") and e not in allowed}
        for road, sources, targets in (
            (P25, ("906417853#0", "906417853#0_p22_east", "huangpu_park_p22"), (P20_SOUTH, P20_LAST)),
            (P26, ("906417852#5", "906417852#6", "906417855#1", "906417855#2"), (P17_MIDDLE, P17_LAST)),
        ):
            for left, right, direction in itertools.product(sources, targets, (0, 1)):
                with self.subTest(road=road, left=left, right=right, direction=direction):
                    a = EdgePosition(left, self.network.edges[left].getLength() / 2)
                    b = EdgePosition(right, self.network.edges[right].getLength() / 2)
                    if direction: a, b = b, a
                    route = self.router.route_via_edges(a, b, (road,), forbidden_edges=forbidden, via_orientations=(direction,))
                    self.assertEqual(1, route.edges.count(road))
                    self.assertTrue(set(route.edges).issubset(allowed))


if __name__ == "__main__":
    unittest.main()
