"""Verify the two requested terminating footways and routes through their shared endpoint."""

import itertools
import json
import math
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from scripts.add_park_p22_p23 import (
    MIDPOINT, TWENTY_PERCENT, JOIN, P15_WEST, P15_EAST, P17_NORTH,
    P17_SOUTH, P22, P23, validate_applied,
)

ROOT = Path(__file__).resolve().parents[1]


def vector(a, b):
    return tuple(b[i] - a[i] for i in (0, 1))


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


class ParkP22P23Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ET.parse(ROOT / "scenarios/shanghai_bund/bund.net.xml").getroot()
        cls.network = ResearchNetwork(str(ROOT / "scenarios/shanghai_bund/bund.net.xml"))
        cls.router = PositionAwarePedestrianRouter(cls.network)
        cls.hotspot = next(h for h in json.loads((ROOT / "config/crowd_hotspots.json").read_text())["hotspots"]
                           if h["id"] == "people_heroes_monument")

    def coord(self, node):
        return self.network.net.getNode(node).getCoord()

    def test_access_points_are_full_p15_midpoint_and_full_p17_twenty_percent(self):
        west, north, south = map(self.coord, ("8417005111", "8417005113", "8417005115"))
        a, b = map(self.coord, (MIDPOINT, TWENTY_PERCENT))
        self.assertLess(math.dist(a, tuple((west[i] + north[i]) / 2 for i in (0, 1))), 0.001)
        p17_south = self.root.find(f"edge[@id='{P17_SOUTH}']")
        shape = [tuple(map(float, p.split(","))) for p in p17_south.get("shape").split()]
        last = self.root.find("edge[@id='906417854_p23_south_p26_south']")
        self.assertEqual(shape[-1], self.coord(last.get("from")))
        self.assertEqual(south, self.coord(last.get("to")))
        self.assertEqual((18570.17, 5792.32), shape[1])  # Keep the existing bend.
        full_length = math.dist(north, shape[1]) + math.dist(shape[1], south)
        self.assertAlmostEqual(0.2, math.dist(north, b) / full_length, places=5)
        self.assertLess(abs(cross(vector(north, shape[1]), vector(north, b))) / math.dist(north, shape[1]), 0.001)

    def test_new_roads_follow_confirmed_directions_and_terminate_at_one_point(self):
        a, b, c = map(self.coord, (MIDPOINT, TWENTY_PERCENT, JOIN))
        west, north = map(self.coord, ("8417005111", "8417005113"))
        d17 = vector(north, (18570.17, 5792.32))
        d15 = vector(west, north)
        self.assertLess(abs(cross(d17, vector(a, c))) / (math.hypot(*d17) * math.dist(a, c)), 0.0001)
        self.assertLess(abs(cross(d15, vector(b, c))) / (math.hypot(*d15) * math.dist(b, c)), 0.0001)
        self.assertGreater(c[0], a[0]); self.assertLess(c[1], a[1])
        self.assertLess(c[0], b[0]); self.assertLess(c[1], b[1])
        self.assertAlmostEqual(16.9563, math.dist(a, c), places=3)
        self.assertAlmostEqual(18.3013, math.dist(b, c), places=3)
        for key, start in ((P22, MIDPOINT), (P23, TWENTY_PERCENT)):
            edge = self.root.find(f"edge[@id='{key}']")
            self.assertEqual((start, JOIN), (edge.get("from"), edge.get("to")))
            lane = edge.find("lane")
            self.assertEqual("pedestrian", lane.get("allow"))
            self.assertEqual(2.0, float(lane.get("width")))
            self.assertEqual(2, len(lane.get("shape").split()))
        incident = {e.get("id") for e in self.root.findall("edge") if JOIN in (e.get("from"), e.get("to"))}
        self.assertEqual({P22, P23, "huangpu_park_p24"}, incident)

    def test_three_new_nodes_have_real_connections_and_widened_walking_areas(self):
        validate_applied(self.root)
        for node, neighbours in (
            (MIDPOINT, {P15_WEST, P15_EAST, P22, "huangpu_park_p25"}),
            (TWENTY_PERCENT, {P17_NORTH, P17_SOUTH, P23}), (JOIN, {P22, P23}),
        ):
            with self.subTest(node=node):
                area = f":{node}_w0"
                self.assertGreaterEqual(float(self.root.find(f"edge[@id='{area}']/lane").get("width")), 4)
                for neighbour in neighbours:
                    # SUMO stores the edge's nominal orientation; pedestrians
                    # may walk those connections in reverse as tested below.
                    edge = self.root.find(f"edge[@id='{neighbour}']")
                    source, target = (neighbour, area) if edge.get("to") == node else (area, neighbour)
                    self.assertIsNotNone(self.root.find(f"connection[@from='{source}'][@to='{target}']"))
                    self.assertIn(neighbour, self.hotspot["park_access_edges"])
                    self.assertIn(neighbour, self.hotspot["visitor_spawn_edges"])
        self.assertEqual(6.0, float(self.root.find("edge[@id=':8417005113_w0']/lane").get("width")))

    def test_both_split_roads_can_use_new_corner_in_both_directions(self):
        allowed = set(self.hotspot["visitor_spawn_edges"])
        forbidden = {e for e in self.router.walkable_edges if not e.startswith(":") and e not in allowed}
        for left, right in itertools.product((P15_WEST, P15_EAST), (P17_NORTH, P17_SOUTH)):
            start = EdgePosition(left, self.network.edges[left].getLength() / 2)
            target = EdgePosition(right, self.network.edges[right].getLength() / 2)
            for first, last, via in ((start, target, (P22, P23)), (target, start, (P23, P22))):
                with self.subTest(left=left, right=right, via=via):
                    route = self.router.route_via_edges(first, last, via, forbidden_edges=forbidden, via_orientations=(0, 1))
                    self.assertEqual(1, route.edges.count(P22))
                    self.assertEqual(1, route.edges.count(P23))
                    self.assertEqual(1, abs(route.edges.index(P22) - route.edges.index(P23)))
                    self.assertTrue(set(route.edges).issubset(allowed))

    def test_existing_reverse_p15_background_walk_uses_east_then_west_half(self):
        root = ET.parse(ROOT / "scenarios/shanghai_bund/bund_ped.rou.xml").getroot()
        route = root.find("person[@id='377']/walk").get("edges").split()
        index = route.index(P15_EAST)
        self.assertEqual(["906417853#1", P15_EAST, P15_WEST, "906417852#10"], route[index - 1:index + 3])


if __name__ == "__main__":
    unittest.main()
