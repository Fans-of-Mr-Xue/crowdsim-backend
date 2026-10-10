"""Approved diagram topology, width invariants, projection and actual routing."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from shapely.geometry import LineString, Point, Polygon

from crowdsim.infrastructure.network_adapter import ResearchNetwork
from scripts.build_east_nanjing_network import build, route_cases

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "scenarios/east_nanjing_road"


class EastNanjingNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads((SCENE / "network_spec.json").read_text())
        cls.xml = ET.parse(SCENE / "east_nanjing.net.xml").getroot()
        cls.network = ResearchNetwork(str(SCENE / "east_nanjing.net.xml"))
        cls.normal = {e.get("id"): e for e in cls.xml.findall("edge")
                      if e.get("function", "normal") == "normal"}
        cls.junctions = {n.get("id"): n for n in cls.xml.findall("junction")
                         if n.get("type") != "internal"}

    def centerline(self, edge):
        if edge.get("shape"):
            return LineString([tuple(map(float, p.split(","))) for p in edge.get("shape").split()])
        return LineString([(float(self.junctions[node].get("x")), float(self.junctions[node].get("y")))
                           for node in [edge.get("from"), edge.get("to")]])

    def test_selected_roads_and_independent_entries_match_the_approved_diagram(self):
        self.assertEqual(31, len(self.normal))
        self.assertEqual({r["id"] for r in self.spec["roads"]}, set(self.normal))
        self.assertEqual({n["id"] for n in self.spec["nodes"]}, set(self.junctions))
        for road in self.spec["roads"]:
            edge = self.normal[road["id"]]
            self.assertEqual((road["from"], road["to"]), (edge.get("from"), edge.get("to")))
            # netconvert drops redundant points (at most 1.5cm here) and
            # omits an edge-level shape for straight node-to-node roads.
            self.assertLess(self.centerline(edge).hausdorff_distance(LineString(road["xy_m"])), .02)
        for node, expected in {
            "J26": {"R01", "R08a", "R08b"}, "J27": {"R03", "R09a", "R09b"},
            "J07": {"R07", "R08b", "R09a"}, "J08": {"R02", "R07"},
            "J11": {"R13", "R25a", "R25b", "R29"},
            "J15": {"R18", "R25b", "R25c", "R30"},
        }.items():
            self.assertEqual(expected, {key for key, edge in self.normal.items()
                                       if node in (edge.get("from"), edge.get("to"))})
        self.assertTrue({"R11", "R19", "R20", "R21", "R27", "R28"}.isdisjoint(self.normal))

    def test_south_paths_stop_at_the_unchanged_preset_boundary(self):
        scope = Polygon([self.network.lonlat_to_xy_strict(*p) for p in self.spec["preset_wgs84_coordinates"]])
        self.assertNotIn("J24", self.junctions)
        for road, node in [("R25c", "J30"), ("R26c", "J31")]:
            junction = self.junctions[node]
            point = Point(float(junction.get("x")), float(junction.get("y")))
            self.assertLess(scope.boundary.distance(point), 1e-6)
            self.assertEqual(node, self.normal[road].get("to"))
            shape = [tuple(map(float, p.split(","))) for p in self.normal[road].get("shape").split()]
            self.assertTrue(all(y >= point.y - 1e-7 for x, y in shape))
        connector = self.normal["R31"]
        self.assertEqual(("J30", "J31"), (connector.get("from"), connector.get("to")))
        shape = self.centerline(connector)
        self.assertTrue(shape.difference(scope.boundary.buffer(1e-6)).is_empty)

    def test_every_real_junction_keeps_minimum_and_inherited_widths(self):
        manifest = json.loads((SCENE / "build_manifest.json").read_text())
        areas = self.xml.findall("edge[@function='walkingarea']")
        self.assertEqual(23, len(areas))
        self.assertEqual(["J20"], manifest["terminal_nodes"])
        by_id = {item["walking_area"]: item for item in manifest["walking_areas"]}
        for area in areas:
            width = float(area.find("lane").get("width"))
            self.assertGreaterEqual(width, 4)
            self.assertGreaterEqual(width, by_id[area.get("id")]["minimum_m"])
            self.assertEqual("pedestrian", area.find("lane").get("allow"))
        for node in ["J11", "J15"]:
            self.assertGreaterEqual(float(self.xml.find(f"edge[@id=':{node}_w0']/lane").get("width")), 5)
        for node in ["J01", "J03", "J19"]:
            self.assertGreaterEqual(float(self.xml.find(f"edge[@id=':{node}_w0']/lane").get("width")), 6.4)
        for edge in self.normal.values():
            self.assertEqual("pedestrian", edge.find("lane").get("allow"))
            self.assertGreater(float(edge.find("lane").get("length")), 0)

    def test_every_ordered_road_pair_and_special_connections_have_a_route(self):
        cases = route_cases(SCENE / "east_nanjing.net.xml", all_pairs=True)
        self.assertEqual(930, sum(case["id"].startswith("pair-") for case in cases))
        self.assertEqual(998, len(cases))
        for case in cases:
            self.assertTrue(set(case["edges"]).issubset(self.normal))
            self.assertGreaterEqual(case["depart_pos"], 0)
            self.assertLessEqual(case["arrival_pos"], self.network.edges[case["edges"][-1]].getLength())
        for case in cases:
            if case["id"].startswith("south-"):
                self.assertIn("R31", case["edges"])
            if "middle" in case["id"]:
                self.assertIn("R07", case["edges"])

    def test_config_and_demo_do_not_hide_bad_routes_or_automatic_jam_resolution(self):
        config = ET.parse(SCENE / "east_nanjing.sumocfg").getroot()
        duration = float(config.find("time/end").get("value"))
        for option in ["pedestrian.striping.jamtime", "pedestrian.striping.jamtime.crossing", "pedestrian.striping.jamtime.narrow"]:
            self.assertGreater(float(config.find(f"processing/{option}").get("value")), duration)
        self.assertEqual("-1", config.find("processing/time-to-teleport").get("value"))
        self.assertEqual("false", config.find("processing/ignore-route-errors").get("value"))
        self.assertEqual(68, len(ET.parse(SCENE / "demo.rou.xml").getroot().findall("person")))

    def test_rebuilding_keeps_the_original_network_and_is_reproducible(self):
        original = ROOT / "scenarios/shanghai_bund/bund.net.xml"
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build(output=output)
            first = (output / "east_nanjing.net.xml").read_bytes()
            self.assertEqual((SCENE / "east_nanjing.net.xml").read_bytes(), first)
            build(output=output)
            self.assertEqual(first, (output / "east_nanjing.net.xml").read_bytes())
        self.assertEqual(before, hashlib.sha256(original.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
