import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from crowdsim.core.population_manager import PopulationManager
from crowdsim.domain.person_parameters import (
    GOAL_LOCK_PARAM,
    HOTSPOT_DWELL_SECONDS_PARAM,
    HOTSPOT_ENTRY_EDGE_PARAM,
    HOTSPOT_ID_PARAM,
    HOTSPOT_PARK_ENTRY_EDGE_PARAM,
    HOTSPOT_RELEASE_TIME_PARAM,
    HOTSPOT_TARGET_EDGE_PARAM,
)
from crowdsim.scenarios.hotspot_demand import LOCK_PARAM, _timeline_alignment, build_hotspot_demand


ROOT = Path(__file__).resolve().parents[1]
BUND = ROOT / "scenarios" / "shanghai_bund"


class HotspotDemandTests(unittest.TestCase):
    def test_timeline_alignment_boundaries(self):
        cases = [
            ((False, [50, 80], False, [240, 800]), (0.0, "disabled")),
            ((True, [], False, [240, 800]), (0.0, "no_visitors")),
            ((True, [50, 80], True, [240, 800]), (0.0, "background_present")),
            ((True, [0, 80], False, [240, 800]), (0.0, "already_aligned")),
            ((True, [50, 80], False, [240, 800]), (50, "aligned")),
            ((True, [50, 80], False, [20, 800]), (20, "limited_by_earlier_time")),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(expected, _timeline_alignment(*arguments))

    def test_alignment_preserves_people_routes_positions_and_all_time_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            config = json.loads((ROOT / "config" / "crowd_hotspots.json").read_text())
            hotspot = next(item for item in config["hotspots"] if item["id"] == "people_heroes_monument")
            # Both counts and seeds can change the first departure; never hardcode 50s.
            for count, seed in [(12, 20260908), (700, 20260908), (12, 20260909)]:
                with self.subTest(count=count, seed=seed):
                    results = []
                    for enabled in (False, True):
                        hotspot["align_first_visitor_to_zero"] = enabled
                        config_path = directory / "config.json"
                        config_path.write_text(json.dumps(config))
                        output = directory / f"{enabled}.xml"
                        report = build_hotspot_demand(
                            BUND / "bund_ped.rou.xml", BUND / "bund.net.xml", config_path, output,
                            hotspot_id=hotspot["id"], visitor_count=count, background_count=0, seed=seed,
                        )
                        people = {p.get("id"): p for p in ET.parse(output).getroot().findall("person")}
                        results.append((report, people))
                    (original, old_people), (aligned, new_people) = results
                    offset = aligned["timeline_shift_seconds"]
                    self.assertGreater(offset, 0)
                    self.assertEqual("aligned", aligned["timeline_alignment_status"])
                    self.assertEqual(original["planned_departure_window_seconds"][0], offset)
                    self.assertEqual(original["planned_departure_window_seconds"], aligned["unshifted_planned_departure_window_seconds"])
                    self.assertEqual(0, aligned["planned_departure_window_seconds"][0])
                    self.assertEqual(0, min(float(p.get("depart")) for p in new_people.values()))
                    self.assertEqual(old_people.keys(), new_people.keys())
                    for key in (
                        "planned_departure_window_seconds", "planned_arrival_window_seconds",
                        "planned_visit_end_window_seconds", "activity_window_seconds", "visitor_release_window_seconds",
                    ):
                        for before, after in zip(original[key], aligned[key]):
                            self.assertGreaterEqual(after, 0)
                            self.assertAlmostEqual(before - offset, after)
                    self.assertEqual(original["configured_visitor_arrival_profile"], aligned["configured_visitor_arrival_profile"])
                    self.assertEqual([600, 800], aligned["configured_activity_window_seconds"])
                    for before, after in zip(original["visitor_arrival_profile"], aligned["visitor_arrival_profile"]):
                        self.assertEqual(before["count"], after["count"])
                        self.assertEqual(before["fraction"], after["fraction"])
                        for start, end in zip(before["window_seconds"], after["window_seconds"]):
                            self.assertAlmostEqual(start - offset, end)
                    for person_id, person in new_people.items():
                        old = old_people[person_id]
                        self.assertAlmostEqual(float(old.get("depart")) - offset, float(person.get("depart")), delta=0.011)
                        for p in person.findall("param"):
                            if p.get("key") == HOTSPOT_RELEASE_TIME_PARAM:
                                old_release = next(q for q in old.findall("param") if q.get("key") == HOTSPOT_RELEASE_TIME_PARAM)
                                self.assertAlmostEqual(float(old_release.get("value")) - offset, float(p.get("value")), delta=0.011)
                                p.set("value", old_release.get("value"))
                        person.set("depart", old.get("depart"))
                        self.assertEqual(ET.tostring(old), ET.tostring(person))

    def test_builds_background_and_locked_multistage_hotspot_visitors(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "hotspot.rou.xml"
            report = build_hotspot_demand(
                BUND / "bund_ped.rou.xml",
                BUND / "bund.net.xml",
                ROOT / "config" / "crowd_hotspots.json",
                output,
                visitor_count=12,
                background_count=20,
            )
            people = ET.parse(output).getroot().findall("person")
            visitors = [person for person in people if person.get("id", "").startswith("hotspot.")]
            background = [person for person in people if not person.get("id", "").startswith("hotspot.")]
            self.assertEqual(32, len(people))
            self.assertEqual(12, len(visitors))
            self.assertGreaterEqual(report["incoming_direction_count"], 2)
            self.assertTrue(all(len(person.findall("walk")) == 2 for person in visitors))
            self.assertTrue(all(len(person.findall("stop")) == 0 for person in visitors))
            self.assertTrue(all(any(
                param.get("key") == HOTSPOT_DWELL_SECONDS_PARAM
                for param in person.findall("param")
            ) for person in visitors))
            self.assertTrue(all(any(param.get("key") == LOCK_PARAM for param in person.findall("param")) for person in visitors))
            self.assertTrue(all(0.0 <= float(person.get("depart")) <= 1199.5 for person in background))
            self.assertEqual(1199.5, max(float(person.get("depart")) for person in background))
            self.assertEqual([0.0, 1199.5], report["background_departure_window_seconds"])
            manager = PopulationManager([output])
            self.assertEqual({person.get("id") for person in visitors}, manager.locked_itinerary_ids)

    def test_population_distinguishes_goal_lock_from_full_itinerary_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "goal-locked.rou.xml"
            root = ET.Element("routes")
            person = ET.SubElement(root, "person", {"id": "visitor", "depart": "0"})
            ET.SubElement(person, "param", {"key": GOAL_LOCK_PARAM, "value": "true"})
            ET.SubElement(person, "param", {"key": HOTSPOT_ID_PARAM, "value": "people_heroes_monument"})
            ET.SubElement(person, "walk", {"edges": "a b"})
            ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)

            manager = PopulationManager([output])

            self.assertNotIn("visitor", manager.locked_itinerary_ids)
            self.assertEqual("people_heroes_monument", manager.goal_locked_hotspot_ids["visitor"])
            self.assertEqual("people_heroes_monument", manager.hotspot_id_for("visitor"))

    def test_monument_visitors_follow_arrival_profile_and_common_event_release(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "monument.rou.xml"
            report = build_hotspot_demand(
                BUND / "bund_ped.rou.xml",
                BUND / "bund.net.xml",
                ROOT / "config" / "crowd_hotspots.json",
                output,
                hotspot_id="people_heroes_monument",
                visitor_count=300,
                background_count=20,
            )
            visitors = [
                person for person in ET.parse(output).getroot().findall("person")
                if person.get("id", "").startswith("hotspot.people_heroes_monument.")
            ]
            excluded = {
                "178411801#1",
                "679361566#1",
                "177931018#1",
                "679361565#1",
                "679361565#2",
                "679361563",
                "679361564",
            }
            targets = set(report["target_edges"])
            self.assertEqual({"679361567#1", "679361567#2"}, targets)
            seen_entries = set()
            seen_departure_entries = set()
            seen_park_entries = set()
            same_entry_departures = 0
            different_entry_departures = 0
            release_times = set()
            viewing_ranges = {
                "679361567#1": ((2.0, 15.0), (15.0, 35.0), (35.0, 90.0),
                                      (90.0, 110.0), (110.0, 119.78)),
                "679361567#2": ((2.0, 17.83),),
            }
            internal_spawn_lengths = {
                "906417851#0": 6.8,
                "906417852#7": 11.7,
                "906417852#8": 26.1,
                "906417852#9": 91.5,
                "906417852#10": 102.9,
            }
            park_entry_edges = {"906417851#0", "906417852#8"}
            park_access_edges = {
                "177931046",
                "906417851#0",
                "906417851#1",
                *(f"906417852#{index}" for index in range(11)),
                "906417853#0",
                "906417853#1",
                "906417854",
                "906417855#1",
                "906417855#2",
            }
            for person in visitors:
                params = {item.get("key"): item.get("value") for item in person.findall("param")}
                target = params[HOTSPOT_TARGET_EDGE_PARAM]
                walks = person.findall("walk")
                inbound = walks[0].get("edges").split()
                outbound = walks[1].get("edges").split()
                self.assertIn(target, targets)
                self.assertEqual(target, inbound[-1])
                self.assertEqual(target, outbound[0])
                if len(outbound) > 1 and params[HOTSPOT_ENTRY_EDGE_PARAM] == outbound[1]:
                    same_entry_departures += 1
                else:
                    different_entry_departures += 1
                self.assertIn(inbound[0], internal_spawn_lengths)
                self.assertIn(outbound[-1], internal_spawn_lengths)
                seen_departure_entries.update(
                    set(outbound) & {"178411801#0", "177931018#0"}
                )
                self.assertIn("departPos", person.attrib)
                depart_position = float(person.get("departPos"))
                self.assertGreaterEqual(depart_position, 2.0)
                self.assertLessEqual(depart_position, internal_spawn_lengths[inbound[0]] - 2.0)
                destination_position = float(walks[1].get("arrivalPos"))
                self.assertGreaterEqual(destination_position, 2.0)
                self.assertLessEqual(
                    destination_position,
                    internal_spawn_lengths[outbound[-1]] - 2.0,
                )
                self.assertNotIn(HOTSPOT_PARK_ENTRY_EDGE_PARAM, params)
                self.assertIn(params[HOTSPOT_ENTRY_EDGE_PARAM], inbound)
                entry_index = inbound.index(params[HOTSPOT_ENTRY_EDGE_PARAM])
                self.assertTrue(set(inbound[:entry_index]).issubset(park_access_edges))
                self.assertFalse((set(inbound) | set(outbound)) & excluded)
                self.assertTrue(any(param.get("key") == GOAL_LOCK_PARAM for param in person.findall("param")))
                self.assertIsNone(person.find("stop"))
                self.assertNotIn(HOTSPOT_DWELL_SECONDS_PARAM, params)
                release_times.add(params[HOTSPOT_RELEASE_TIME_PARAM])
                target_position = float(walks[0].get("arrivalPos"))
                self.assertTrue(any(
                    start <= target_position <= end
                    for start, end in viewing_ranges[target]
                ))
                seen_entries.update(set(inbound) & {"178411801#0", "177931018#0"})
                if HOTSPOT_PARK_ENTRY_EDGE_PARAM in params:
                    seen_park_entries.add(params[HOTSPOT_PARK_ENTRY_EDGE_PARAM])

            self.assertEqual(300, len(visitors))
            self.assertEqual(300, sum(report["target_edge_counts"].values()))
            self.assertTrue(all(count > 0 for count in report["target_edge_counts"].values()))
            self.assertEqual({"178411801#0", "177931018#0"}, seen_entries)
            self.assertEqual({"178411801#0", "177931018#0"}, seen_departure_entries)
            self.assertGreater(same_entry_departures, 0)
            self.assertGreater(different_entry_departures, 0)
            self.assertEqual(set(), seen_park_entries)
            self.assertEqual(park_entry_edges, set(report["park_entry_counts"]))
            self.assertTrue(all(count == 0 for count in report["park_entry_counts"].values()))
            self.assertEqual(300, sum(report["spawn_edge_counts"].values()))
            self.assertEqual(set(internal_spawn_lengths), set(report["spawn_edge_counts"]))
            self.assertTrue(all(count > 0 for count in report["spawn_edge_counts"].values()))
            self.assertEqual("edge_uniform_random", report["destination_distribution"])
            self.assertEqual(
                {edge_id: 60 for edge_id in internal_spawn_lengths},
                report["destination_edge_counts"],
            )
            self.assertEqual(300, sum(report["departure_entry_counts"].values()))
            self.assertTrue(all(count > 0 for count in report["departure_entry_counts"].values()))
            self.assertGreater(report["same_spawn_destination_edge_count"], 0)
            self.assertLess(report["same_spawn_destination_edge_count"], 300)
            self.assertEqual(0.0, report["estimated_park_entry_time_seconds"]["maximum"])
            self.assertEqual(0, report["departure_clamped_count"])
            self.assertEqual("target_arrival_profile", report["visitor_schedule_mode"])
            self.assertIsNone(report["configured_visitor_departure_window_seconds"])
            self.assertIsNone(report["configured_arrival_window_seconds"])
            self.assertEqual([30, 225, 45], [
                segment["count"] for segment in report["configured_visitor_arrival_profile"]
            ])
            self.assertEqual([240.0, 660.0], report["planned_arrival_window_seconds"])
            self.assertEqual([600.0, 800.0], report["activity_window_seconds"])
            self.assertEqual([800.0, 890.0], report["visitor_release_window_seconds"])
            self.assertEqual(
                {
                    "east_best_view": 165,
                    "north_secondary_view": 45,
                    "south_secondary_view": 45,
                    "northwest_overflow": 15,
                    "southwest_overflow": 12,
                    "west_back_overflow": 18,
                },
                report["viewing_zone_counts"],
            )
            departures = sorted(float(person.get("depart")) for person in visitors)
            self.assertGreaterEqual(departures[0], 0.0)
            self.assertLess(departures[-1], 660.0)
            self.assertGreater(len(set(departures)), 290)
            self.assertGreater(len(release_times), 290)
            self.assertEqual([800.0, 890.0], report["planned_visit_end_window_seconds"])
            self.assertEqual("common_event_release_hold", report["dwell_model"])
            manager = PopulationManager([output])
            self.assertEqual(300, len(manager.hotspot_target_edges))
            self.assertEqual(300, len(manager.hotspot_target_positions))
            self.assertEqual(0, len(manager.hotspot_dwell_seconds))
            self.assertEqual(300, len(manager.hotspot_release_times))
            self.assertEqual(300, len(manager.hotspot_initial_entry_edges))
            self.assertEqual(0, len(manager.hotspot_park_entry_edges))
            self.assertTrue(set(manager.hotspot_target_edges.values()).issubset(targets))
            for person in visitors:
                self.assertEqual(
                    float(person.find("walk").get("arrivalPos")),
                    manager.hotspot_target_position_for(person.get("id")),
                )
                params = {item.get("key"): item.get("value") for item in person.findall("param")}
                self.assertEqual(
                    float(params[HOTSPOT_RELEASE_TIME_PARAM]),
                    manager.hotspot_release_times[person.get("id")],
                )

    def test_population_rejects_inconsistent_hotspot_walk_and_stop_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bad-position.rou.xml"
            root = ET.Element("routes")
            person = ET.SubElement(root, "person", {"id": "visitor", "depart": "0"})
            ET.SubElement(person, "param", {"key": GOAL_LOCK_PARAM, "value": "true"})
            ET.SubElement(person, "param", {"key": HOTSPOT_ID_PARAM, "value": "monument"})
            ET.SubElement(person, "param", {"key": HOTSPOT_TARGET_EDGE_PARAM, "value": "ring"})
            ET.SubElement(person, "walk", {"edges": "outside ring", "arrivalPos": "4"})
            ET.SubElement(person, "stop", {
                "lane": "ring_0", "endPos": "5", "duration": "10", "actType": "hotspot_visit",
            })
            ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)

            with self.assertRaisesRegex(ValueError, "inconsistent walk/stop positions"):
                PopulationManager([output])


if __name__ == "__main__":
    unittest.main()
