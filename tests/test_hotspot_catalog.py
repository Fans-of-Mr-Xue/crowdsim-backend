import copy
import unittest
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

from crowdsim.decision.pedestrian_reachability import PedestrianReachability
from crowdsim.environment.hotspot_catalog import HotspotCatalog
from crowdsim.infrastructure.network_adapter import ResearchNetwork


ROOT = Path(__file__).resolve().parents[1]


class HotspotCatalogTests(unittest.TestCase):
    def test_generated_timing_replaces_catalog_clock_without_shifting_twice(self):
        catalog = copy.deepcopy(self.catalog)
        report = {
            "hotspot_id": "people_heroes_monument",
            "visitor_departure_window_seconds": None,
            "visitor_arrival_profile": [{"window_seconds": [190, 310], "fraction": 1.0, "count": 12}],
            "arrival_window_seconds": None,
            "activity_window_seconds": [550, 750],
            "timeline_shift_seconds": 50.0,
            "timeline_alignment_status": "aligned",
        }
        for _ in range(2):
            catalog.apply_demand_timing(report)
            serialized = next(item for item in catalog.serialize() if item["id"] == report["hotspot_id"])
            self.assertEqual([550, 750], serialized["activity_window_seconds"])
            self.assertEqual(50.0, serialized["timeline_shift_seconds"])
            self.assertEqual(report["visitor_arrival_profile"], serialized["visitor_arrival_profile"])
        report["activity_window_seconds"][0] = 123
        self.assertEqual([550, 750], catalog.hotspots[report["hotspot_id"]]["activity_window_seconds"])

    @classmethod
    def setUpClass(cls):
        cls.network = ResearchNetwork(str(ROOT / "scenarios" / "shanghai_bund" / "bund.net.xml"))
        cls.catalog = HotspotCatalog(cls.network, ROOT / "config" / "crowd_hotspots.json")
        cls.reachability = PedestrianReachability(cls.network)

    @classmethod
    def can_reach_within(cls, from_edge, to_edge, allowed_regular_edges):
        allowed = set(allowed_regular_edges)
        allowed.update(edge_id for edge_id in cls.reachability.walkable if edge_id.startswith(":"))
        queue = deque([from_edge])
        reached = {from_edge}
        while queue:
            edge_id = queue.popleft()
            if edge_id == to_edge:
                return True
            for neighbour in cls.reachability.neighbours[edge_id]:
                if neighbour in allowed and neighbour not in reached:
                    reached.add(neighbour)
                    queue.append(neighbour)
        return False

    def test_monument_uses_only_outer_ground_level_ring_as_target_zone(self):
        hotspot = self.catalog.hotspots["people_heroes_monument"]

        self.assertEqual("679361567#1", hotspot["target_edge"])
        self.assertEqual(
            {
                "679361567#1",
                "679361567#2",
            },
            set(hotspot["target_edges"]),
        )
        self.assertEqual(
            {
                "178411801#1",
                "679361566#1",
                "177931018#1",
                "679361565#1",
                "679361565#2",
                "679361563",
                "679361564",
            },
            set(hotspot["excluded_edges"]),
        )
        self.assertTrue(set(hotspot["target_edges"]).issubset(hotspot["measurement_edges"]))
        self.assertTrue(set(hotspot["target_edges"]).isdisjoint(hotspot["excluded_edges"]))

    def test_all_configured_monument_edges_exist_and_allow_pedestrians(self):
        hotspot = self.catalog.hotspots["people_heroes_monument"]

        for edge_id in (*hotspot["target_edges"], *hotspot["excluded_edges"]):
            edge = self.network.edges[edge_id]
            self.assertTrue(any(lane.allows("pedestrian") for lane in edge.getLanes()), edge_id)

    def test_serialized_hotspot_exposes_multi_edge_zone(self):
        hotspot = next(
            item for item in self.catalog.serialize() if item["id"] == "people_heroes_monument"
        )

        self.assertEqual(2, len(hotspot["target_edges"]))
        self.assertEqual(7, len(hotspot["excluded_edges"]))
        self.assertEqual(2, len(hotspot["entry_edges"]))
        self.assertEqual(hotspot["park_access_edges"], hotspot["approach_edges"])
        self.assertEqual(
            hotspot["entry_edges"],
            [portal["edge"] for portal in hotspot["access_portals"]],
        )
        self.assertEqual(19, len(hotspot["park_access_edges"]))
        self.assertEqual(2, len(hotspot["park_entry_edges"]))
        self.assertEqual(
            {
                "906417851#0",
                "906417852#7",
                "906417852#8",
                "906417852#9",
                "906417852#10",
            },
            set(hotspot["visitor_spawn_edges"]),
        )
        self.assertEqual("edge_length_weighted_random", hotspot["spawn_distribution"])
        self.assertEqual(2.0, hotspot["spawn_position_margin_meters"])
        self.assertEqual(
            set(hotspot["visitor_spawn_edges"]),
            set(hotspot["visitor_destination_edges"]),
        )
        self.assertEqual("edge_uniform_random", hotspot["destination_distribution"])
        self.assertEqual(2.0, hotspot["destination_position_margin_meters"])
        self.assertEqual([], hotspot["visitor_exit_edges"])
        self.assertIsNone(hotspot["visitor_departure_window_seconds"])
        self.assertEqual("weighted_viewing_arcs", hotspot["target_distribution"])
        self.assertEqual(6, len(hotspot["viewing_zones"]))
        self.assertAlmostEqual(
            1.0,
            sum(zone["weight"] for zone in hotspot["viewing_zones"]),
        )
        self.assertEqual([600.0, 800.0], hotspot["activity_window_seconds"])
        self.assertEqual([0.0, 90.0], hotspot["visitor_release_delay_seconds"])
        self.assertEqual(
            [0.10, 0.75, 0.15],
            [segment["fraction"] for segment in hotspot["visitor_arrival_profile"]],
        )
        self.assertEqual(0, self.catalog.hotspots["people_heroes_monument"]["background_count"])
        self.assertEqual(18, len(hotspot["external_approach_edges"]))
        self.assertTrue(set(hotspot["visitor_spawn_edges"]).issubset(hotspot["park_access_edges"]))
        self.assertTrue(
            set(hotspot["visitor_spawn_edges"]).isdisjoint(hotspot["external_approach_edges"])
        )
        self.assertTrue(hotspot["is_default"])

    def test_park_access_network_reaches_both_entrances_in_both_directions(self):
        hotspot = self.catalog.hotspots["people_heroes_monument"]
        allowed = (*hotspot["park_access_edges"], *hotspot["entry_edges"])

        for park_edge in hotspot["park_access_edges"]:
            for entry_edge in hotspot["entry_edges"]:
                self.assertTrue(
                    self.can_reach_within(park_edge, entry_edge, allowed),
                    f"{park_edge} cannot reach {entry_edge} within the configured park network",
                )
                self.assertTrue(
                    self.can_reach_within(entry_edge, park_edge, allowed),
                    f"{entry_edge} cannot reach {park_edge} within the configured park network",
                )

    def test_both_entrances_reach_every_ground_target_edge_in_both_directions(self):
        hotspot = self.catalog.hotspots["people_heroes_monument"]
        allowed = (*hotspot["entry_edges"], *hotspot["target_edges"])

        for entry_edge in hotspot["entry_edges"]:
            for target_edge in hotspot["target_edges"]:
                self.assertTrue(
                    self.can_reach_within(entry_edge, target_edge, allowed),
                    f"{entry_edge} cannot reach {target_edge}",
                )
                self.assertTrue(
                    self.can_reach_within(target_edge, entry_edge, allowed),
                    f"{target_edge} cannot reach {entry_edge}",
                )

    def test_all_park_and_monument_entry_walking_areas_are_at_least_four_meters_wide(self):
        net_root = ET.parse(ROOT / "scenarios" / "shanghai_bund" / "bund.net.xml").getroot()
        hotspot = self.catalog.hotspots["people_heroes_monument"]
        scoped_edges = set(hotspot["park_access_edges"]) | set(hotspot["entry_edges"])
        connections = [
            (connection.attrib["from"], connection.attrib["to"])
            for connection in net_root.findall("./connection")
        ]
        walking_areas = {
            edge.attrib["id"]: edge
            for edge in net_root.findall("./edge[@function='walkingarea']")
            if any(
                edge.attrib["id"] in pair
                and (pair[0] in scoped_edges or pair[1] in scoped_edges)
                for pair in connections
            )
        }
        self.assertEqual(
            {
                ":1883306110_w0",
                ":1883306436_w0",
                ":595760221_w0",
                ":599722293_w0",
                ":6361541057_w0",
                ":6361541058_w0",
                ":8417005097_w0",
                ":8417005101_w0",
                ":8417005102_w0",
                ":8417005103_w0",
                ":8417005104_w0",
                ":8417005105_w0",
                ":8417005110_w0",
                ":8417005111_w0",
                ":8417005112_w0",
                ":8417005113_w0",
                ":8417005115_w0",
            },
            set(walking_areas),
        )

        for edge_id, walking_area in walking_areas.items():
            with self.subTest(edge_id=edge_id):
                lane = walking_area.find(f"./lane[@id='{edge_id}_0']")
                self.assertIsNotNone(lane)
                self.assertGreaterEqual(float(lane.attrib["width"]), 4.0)

    def test_hotspot_jam_squeezing_stays_disabled_for_the_full_timeline(self):
        config = ET.parse(
            ROOT / "scenarios" / "shanghai_bund" / "bund.hotspot.sumocfg"
        ).getroot()
        timeline_end = float(config.find("./time/end").attrib["value"])

        for option in (
            "pedestrian.striping.jamtime",
            "pedestrian.striping.jamtime.crossing",
            "pedestrian.striping.jamtime.narrow",
        ):
            with self.subTest(option=option):
                threshold = float(config.find(f"./processing/{option}").attrib["value"])
                self.assertGreater(threshold, timeline_end)

    def test_existing_single_edge_hotspot_remains_compatible(self):
        hotspot = self.catalog.hotspots["chen_yi_square"]

        self.assertEqual("40301368#10", hotspot["target_edge"])
        self.assertEqual(("40301368#10",), hotspot["target_edges"])


if __name__ == "__main__":
    unittest.main()
