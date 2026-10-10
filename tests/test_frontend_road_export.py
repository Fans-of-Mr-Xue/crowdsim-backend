"""Road export geometry and projection checks; does not start SUMO."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from scripts.export_frontend_road_network import build_road_network, export_road_network


ROOT = Path(__file__).resolve().parents[1]
HAS_GEOMETRY = all(importlib.util.find_spec(name) is not None for name in ("pyproj", "shapely"))

FIXTURE = '''<net version="1.20">
  <location netOffset="-337299.12,-3451800.16"
    projParameter="+proj=utm +zone=51 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"/>
  <edge id="road" from="a" to="b" type="highway.footway">
    <lane id="road_0" width="2" length="10" shape="0,0 10,0"/>
  </edge>
  <edge id=":b_w0" function="walkingarea">
    <lane id=":b_w0_0" width="4" length="4" shape="10,-2 14,-2 14,2 10,2"/>
  </edge>
  <edge id=":unused_0" function="internal">
    <lane id=":unused_0_0" width="100" length="10" shape="1000,0 1010,0"/>
  </edge>
  <junction id="a" type="dead_end" x="0" y="0" shape="0,-1 0,1"/>
  <junction id="b" type="dead_end" x="12" y="0" shape="10,-2 14,-2 14,2 10,2"/>
</net>'''


@unittest.skipUnless(HAS_GEOMETRY, "optional requirements-road-export.txt dependencies are required")
class RoadExportFixtureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.network = Path(self.directory.name) / "fixture.net.xml"
        self.network.write_text(FIXTURE)

    def test_widths_walking_polygons_and_projection_preserve_actual_surfaces(self):
        from pyproj import Transformer
        from shapely.geometry import Polygon
        payload = build_road_network(self.network)
        to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32651", always_xy=True)
        local = []
        for lng, lat in payload["roadLines"][0]:
            x, y = to_utm.transform(lng, lat)
            local.append((x - 337299.12, y - 3451800.16))
        polygon = Polygon(local)
        # 10m * 2m lane + the actual 4m * 4m junction polygon. A walking
        # area's width must not be used to buffer its already complete shape.
        self.assertAlmostEqual(36, polygon.area, delta=0.01)
        for actual, expected in zip(polygon.bounds, (0, -2, 14, 2)):
            self.assertAlmostEqual(expected, actual, delta=0.001)
        self.assertEqual(1, payload["counts"]["normalEdges"])
        self.assertEqual(1, payload["counts"]["walkingAreas"])
        self.assertEqual(4, payload["walkingAreas"][0]["widthMeters"])
        self.assertEqual("lng,lat", payload["coordinateOrder"])

    def test_export_is_reproducible_and_never_changes_source_network(self):
        original = self.network.read_bytes()
        output = Path(self.directory.name) / "export/road_network.json"
        payload = export_road_network(self.network, output)
        first = output.read_bytes()
        export_road_network(self.network, output)
        self.assertEqual(first, output.read_bytes())
        self.assertEqual(original, self.network.read_bytes())
        self.assertEqual(hashlib.sha256(original).hexdigest(), payload["source"]["sha256"])
        self.assertEqual(payload, json.loads(first))
        with self.assertRaisesRegex(ValueError, "overwrite"):
            export_road_network(self.network, self.network)

    def test_missing_projection_cannot_be_mistaken_for_geographic_coordinates(self):
        self.network.write_text(FIXTURE.replace(
            "+proj=utm +zone=51 +ellps=WGS84 +datum=WGS84 +units=m +no_defs", "!"
        ))
        with self.assertRaisesRegex(ValueError, "projection"):
            build_road_network(self.network)

    def test_two_point_walking_areas_keep_metadata_without_inventing_a_polygon(self):
        self.network.write_text(FIXTURE.replace("10,-2 14,-2 14,2 10,2", "10,-2 10,2", 1))
        payload = build_road_network(self.network)
        self.assertEqual(1, payload["counts"]["degenerateWalkingAreas"])
        self.assertEqual(1, payload["counts"]["walkingAreas"])
        self.assertEqual(2, len(payload["walkingAreas"][0]["path"]))


@unittest.skipUnless(HAS_GEOMETRY, "optional requirements-road-export.txt dependencies are required")
class CurrentBundRoadExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network_path = ROOT / "scenarios/shanghai_bund/bund.net.xml"
        cls.payload = build_road_network(cls.network_path)
        cls.xml = ET.parse(cls.network_path).getroot()

    def test_m3_split_h2_and_widened_walking_areas_match_current_network(self):
        from crowdsim.infrastructure.network_adapter import ResearchNetwork
        network = ResearchNetwork(str(self.network_path))
        exported = {edge["edgeId"]: edge for edge in self.payload["roadSegments"]}
        for identifier in (
            "monument_m3", "679361567#2", "679361567#2_m3_north",
            "huangpu_park_j05_j15", "huangpu_park_j05_j15_p21_south",
            "906417852#10", "906417852#10_p21_north", "huangpu_park_p21",
            "906417853#0", "906417853#0_p22_east", "906417854", "906417854_p23_south",
            "huangpu_park_p22", "huangpu_park_p23",
            "906417852#9", "906417852#9_p24_north", "huangpu_park_p24",
            "huangpu_park_p25", "huangpu_park_p26",
            "huangpu_park_p27_a", "huangpu_park_p27_b", "huangpu_park_p27_c",
            "huangpu_park_p28",
            "huangpu_park_j05_j15_p21_south_p25_south", "906417854_p23_south_p26_south",
        ):
            xml_edge = self.xml.find(f"edge[@id='{identifier}']")
            self.assertEqual(xml_edge.get("from"), exported[identifier]["fromJunction"])
            self.assertEqual(xml_edge.get("to"), exported[identifier]["toJunction"])
            for lane, xml_lane in zip(exported[identifier]["lanes"], xml_edge.findall("lane")):
                points = [tuple(map(float, point.split(","))) for point in xml_lane.get("shape").split()]
                self.assertEqual(len(points), len(lane["path"]))
                self.assertEqual(float(xml_lane.get("width")), lane["widthMeters"])
                for lnglat, xy in zip(lane["path"], points):
                    actual = network.lonlat_to_xy(*lnglat)
                    self.assertLess(sum((actual[i] - xy[i]) ** 2 for i in (0, 1)) ** 0.5, 0.001)
        areas = {area["edgeId"]: area for area in self.payload["walkingAreas"]}
        for edge in self.xml.findall("edge"):
            if edge.get("function") == "walkingarea":
                area = areas[edge.get("id")]
                self.assertEqual(float(edge.find("lane").get("width")), area["widthMeters"])
                self.assertEqual(len(edge.find("lane").get("shape").split()), len(area["path"]))
        self.assertEqual(4, areas[":monument_m3_ring_junction_w0"]["widthMeters"])
        self.assertEqual(4, areas[":8417005112_w0"]["widthMeters"])
        self.assertEqual(5, areas[":6361541057_w0"]["widthMeters"])

    def test_merged_outline_includes_the_new_m3_corridor(self):
        from crowdsim.infrastructure.network_adapter import ResearchNetwork
        from shapely.geometry import LineString, Point
        network = ResearchNetwork(str(self.network_path))
        midpoint = Point(18591.185, 5845.89)
        distances = [
            LineString([network.lonlat_to_xy(*point) for point in line]).distance(midpoint)
            for line in self.payload["roadLines"]
        ]
        # The old outline was 4.639m away here. The 2m wide new M3 now has
        # its own boundary within 1m of its centerline.
        self.assertLessEqual(min(distances), 1.01)
        for line in self.payload["roadLines"]:
            self.assertGreaterEqual(len(line), 4)
            self.assertEqual(line[0], line[-1])


if __name__ == "__main__":
    unittest.main()
