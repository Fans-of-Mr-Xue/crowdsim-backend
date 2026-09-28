from pathlib import Path
from types import SimpleNamespace
import unittest

from traci import constants as tc

from crowdsim.domain.crowdsim_models import AgentState, MotionSnapshot
from crowdsim.environment.hotspot_catalog import HotspotCatalog
from crowdsim.infrastructure.metrics import MetricsCollector
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.sumo_adapter import SumoStepResult


ROOT = Path(__file__).resolve().parents[1]
BUND = ROOT / "scenarios" / "shanghai_bund"


def motion(person_id, edge_id, *, speed=1.0, stage_type=tc.STAGE_WALKING, remaining=3):
    return MotionSnapshot(
        person_id=person_id,
        time_seconds=100,
        x=0,
        y=0,
        lon=0,
        lat=0,
        speed=speed,
        edge_id=edge_id,
        lane_id=edge_id + "_0",
        lane_position=1,
        angle=0,
        stage_index=0,
        stage_type=stage_type,
        remaining_stage_count=remaining,
    )


class HotspotMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = ResearchNetwork(str(BUND / "bund.net.xml"))
        cls.catalog = HotspotCatalog(cls.network, ROOT / "config" / "crowd_hotspots.json")

    def test_monument_core_and_visit_lifecycle_use_the_outer_ring(self):
        visitor_ids = {name: "people_heroes_monument" for name in (
            "outside", "queued", "entering", "gathering", "departing", "completed"
        )}
        manager = SimpleNamespace(
            hotspot_ids=visitor_ids,
            ledger=SimpleNamespace(
                departed_ids=set(visitor_ids),
                arrived_ids={"completed"},
            ),
        )
        collector = MetricsCollector(
            self.network,
            hotspots=self.catalog,
            population_manager=manager,
        )
        persons = {
            "outside": motion("outside", "47438461#1", speed=0.1),
            "queued": motion("queued", "906417852#5", speed=0.1),
            "entering": motion("entering", "178411801#0"),
            "gathering": motion("gathering", "679361567#2", speed=0, remaining=1),
            "departing": motion("departing", "906417853#1", remaining=1),
        }
        states = {
            person_id: AgentState(person_id, blocked_duration=6 if person_id == "queued" else 0)
            for person_id in persons
        }
        states["gathering"].activity_state = "hotspot_dwelling"
        states["departing"].activity_state = "hotspot_departing"
        measured = collector.measure(
            SumoStepResult(100, persons, {}, tuple(persons), ()),
            states,
            {},
        )["hotspots"]["people_heroes_monument"]

        self.assertEqual(1, measured["core"]["person_count"])
        self.assertEqual(2, len(measured["core"]["target_edges"]))
        self.assertEqual(1, measured["core"]["by_target_edge"]["679361567#2"]["person_count"])
        self.assertEqual(1, measured["entries"]["person_count"])
        self.assertEqual(2, measured["park"]["person_count"])
        self.assertEqual(1, measured["park"]["queued_hotspot_visitor_count"])
        self.assertEqual(1, measured["external_approach"]["person_count"])
        self.assertEqual(1, measured["external_approach"]["queued_hotspot_visitor_count"])
        lifecycle = measured["visit_lifecycle"]
        self.assertEqual(1, lifecycle["queued_in_park_count"])
        self.assertEqual(1, lifecycle["queued_outside_park_count"])
        self.assertEqual(1, lifecycle["entering_count"])
        self.assertEqual(1, lifecycle["gathering_count"])
        self.assertEqual(1, lifecycle["departing_count"])
        self.assertEqual(1, lifecycle["completed_count"])
        self.assertEqual(3, lifecycle["arrived_at_target_count"])
        self.assertEqual(5, lifecycle["remaining_visitor_count"])
        self.assertEqual(4, lifecycle["network_backlog_count"])
        self.assertEqual(2, lifecycle["blocked_network_count"])
        self.assertAlmostEqual(1 / 6, lifecycle["completion_fraction"])
        self.assertEqual(5, measured["process_state"]["remaining_visitor_count"])

    def test_entry_flow_ignores_internal_junction_edges(self):
        collector = MetricsCollector(self.network, hotspots=self.catalog)
        states = {"p": AgentState("p")}
        sequence = (
            motion("p", "906417852#0"),
            motion("p", ":internal"),
            motion("p", "178411801#0"),
        )
        measured = None
        for index, snapshot in enumerate(sequence):
            measured = collector.measure(
                SumoStepResult(index, {"p": snapshot}, {}, ("p",) if index == 0 else (), ()),
                states,
                {},
            )

        entries = measured["hotspots"]["people_heroes_monument"]["entries"]
        self.assertEqual(1, entries["inbound_crossing_count"])
        self.assertEqual(0, entries["outbound_crossing_count"])

    def test_park_entry_flow_counts_external_to_internal_crossing(self):
        collector = MetricsCollector(self.network, hotspots=self.catalog)
        states = {"p": AgentState("p")}
        sequence = (
            motion("p", "178564465#2"),
            motion("p", ":park_gate"),
            motion("p", "906417851#0"),
        )
        measured = None
        for index, snapshot in enumerate(sequence):
            measured = collector.measure(
                SumoStepResult(index, {"p": snapshot}, {}, ("p",) if index == 0 else (), ()),
                states,
                {},
            )

        park_entries = measured["hotspots"]["people_heroes_monument"]["park_entries"]
        self.assertEqual(1, park_entries["inbound_crossing_count"])
        self.assertEqual(0, park_entries["outbound_crossing_count"])


if __name__ == "__main__":
    unittest.main()
