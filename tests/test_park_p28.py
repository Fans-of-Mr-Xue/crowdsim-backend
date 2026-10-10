"""Northeast perpendicular geometry, full-road half length and terminal access."""

import json
import math
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from scripts.add_park_p28 import (
    P28, TERMINUS, P20_MIDPOINT, P20_SOUTH, P20_LAST, P25, EAST,
    J15, LENGTH_REFERENCE, full_centerline, validate_applied,
)

ROOT = Path(__file__).resolve().parents[1]


class ParkP28Tests(unittest.TestCase):
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

    def test_northeast_road_is_perpendicular_and_half_the_full_j15_j13_length(self):
        start, end, j15, j28 = map(self.coord, (P20_MIDPOINT, TERMINUS, J15, EAST))
        branch = tuple(end[i]-start[i] for i in (0,1))
        reference_direction = tuple(j28[i]-j15[i] for i in (0,1))
        cosine = abs(sum(branch[i]*reference_direction[i] for i in (0,1))) / (math.hypot(*branch)*math.hypot(*reference_direction))
        self.assertLess(cosine, 0.00005)
        self.assertGreater(branch[0], 0); self.assertGreater(branch[1], 0)
        points = full_centerline(self.root, LENGTH_REFERENCE)
        full_length = sum(math.dist(a,b) for a,b in zip(points,points[1:]))
        self.assertAlmostEqual(full_length/2, math.dist(start,end), delta=0.001)
        self.assertAlmostEqual(17.254394, math.dist(start,end), delta=0.001)
        # A lane is trimmed by its two walkingareas; its reported length is not
        # the junction-to-junction reference requested by the user.
        self.assertGreater(full_length-float(self.root.find(f"edge[@id='{LENGTH_REFERENCE}']/lane").get('length')), 4)

    def test_new_footway_is_straight_two_meters_and_ends_only_at_j37(self):
        edge = self.root.find(f"edge[@id='{P28}']")
        self.assertEqual((P20_MIDPOINT,TERMINUS), (edge.get('from'),edge.get('to')))
        lane = edge.find('lane')
        self.assertEqual('pedestrian',lane.get('allow'))
        self.assertEqual(2,float(lane.get('width')))
        self.assertEqual(2,len(lane.get('shape').split()))
        incident = {e.get('id') for e in self.root.findall('edge') if TERMINUS in (e.get('from'),e.get('to'))}
        self.assertEqual({P28},incident)
        self.assertEqual('dead_end',self.root.find(f"junction[@id='{TERMINUS}']").get('type'))

    def test_j33_is_a_real_widened_four_arm_junction_and_branch_is_in_scope(self):
        validate_applied(self.root)
        area = f':{P20_MIDPOINT}_w0'
        self.assertGreaterEqual(float(self.root.find(f"edge[@id='{area}']/lane").get('width')),4)
        for key in (P20_SOUTH,P20_LAST,P25,P28):
            edge = self.root.find(f"edge[@id='{key}']")
            source,target = (key,area) if edge.get('to')==P20_MIDPOINT else (area,key)
            self.assertIsNotNone(self.root.find(f"connection[@from='{source}'][@to='{target}']"))
            self.assertIn(key,self.hotspot['park_access_edges'])
            self.assertIn(key,self.hotspot['visitor_spawn_edges'])

    def test_all_existing_approaches_can_enter_the_terminal_branch_and_return(self):
        allowed = set(self.hotspot['visitor_spawn_edges'])
        forbidden = {e for e in self.router.walkable_edges if not e.startswith(':') and e not in allowed}
        near_end = EdgePosition(P28,self.network.edges[P28].getLength()-.25)
        for key in (P20_SOUTH,P20_LAST,P25):
            with self.subTest(approach=key):
                point = EdgePosition(key,self.network.edges[key].getLength()/2)
                inbound = self.router.route(point,near_end,forbidden_edges=forbidden)
                outbound = self.router.route(near_end,point,forbidden_edges=forbidden)
                self.assertEqual(P28,inbound.edges[-1])
                self.assertEqual(P28,outbound.edges[0])
                self.assertTrue(set(inbound.edges+outbound.edges).issubset(allowed))


if __name__ == '__main__':
    unittest.main()
