from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from traci import constants as tc

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.domain.crowdsim_models import AgentState, MotionSnapshot
from crowdsim.infrastructure.sumo_adapter import SumoStepResult
from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary
from crowdsim.scenarios.hotspot_demand import build_hotspot_demand


ROOT = Path(__file__).resolve().parents[1]
BUND = ROOT / "scenarios" / "shanghai_bund"


def has_sumo() -> bool:
    try:
        discover_sumo_binary()
        return True
    except FileNotFoundError:
        return False


def motion(*, remaining_stage_count: int) -> MotionSnapshot:
    return MotionSnapshot(
        person_id="visitor",
        time_seconds=10.0,
        x=1.0,
        y=2.0,
        lon=0.0,
        lat=0.0,
        speed=1.0,
        edge_id="ring",
        lane_id="ring_0",
        lane_position=5.0,
        angle=0.0,
        stage_index=0,
        stage_type=tc.STAGE_WALKING,
        remaining_stage_count=remaining_stage_count,
    )


class FakeExecutor:
    def __init__(self):
        self.activity_hold_until = {}
        self.holds = []
        self.releases = []

    def hold_activity(self, person_id, until):
        self.activity_hold_until[person_id] = until
        self.holds.append((person_id, until))

    def release_activity_hold(self, person_id):
        self.activity_hold_until.pop(person_id, None)
        self.releases.append(person_id)


class HotspotDwellRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = SimulationRuntime.__new__(SimulationRuntime)
        self.runtime.plan_executor = FakeExecutor()
        self.runtime.population = SimpleNamespace(
            hotspot_ids={"visitor": "monument"},
            hotspot_target_edges={"visitor": "ring"},
            hotspot_dwell_seconds={"visitor": 30.0},
            hotspot_release_times={},
            states={"visitor": AgentState("visitor")},
        )

    def test_hold_starts_only_after_arrival_walk_switches_to_outbound_walk(self):
        approaching = SumoStepResult(9.5, {"visitor": motion(remaining_stage_count=2)}, {}, (), ())
        self.runtime._update_hotspot_activity(approaching)
        state = self.runtime.population.states["visitor"]
        self.assertEqual("hotspot_approaching", state.activity_state)
        self.assertEqual([], self.runtime.plan_executor.holds)

        arrived = SumoStepResult(10.0, {"visitor": motion(remaining_stage_count=1)}, {}, (), ())
        self.runtime._update_hotspot_activity(arrived)

        self.assertEqual([("visitor", 40.0)], self.runtime.plan_executor.holds)
        self.assertEqual("hotspot_dwelling", state.activity_state)
        self.assertEqual(40.0, state.hotspot_dwell_until)
        self.assertEqual(40.0, state.next_decision_time)

    def test_common_event_release_uses_absolute_time_instead_of_arrival_relative_dwell(self):
        self.runtime.population.hotspot_release_times["visitor"] = 100.0

        arrived = SumoStepResult(70.0, {"visitor": motion(remaining_stage_count=1)}, {}, (), ())
        self.runtime._update_hotspot_activity(arrived)

        state = self.runtime.population.states["visitor"]
        self.assertEqual([("visitor", 100.0)], self.runtime.plan_executor.holds)
        self.assertEqual(100.0, state.hotspot_dwell_until)
        self.assertEqual(100.0, state.next_decision_time)

    def test_completed_hold_resumes_same_walking_stage(self):
        state = self.runtime.population.states["visitor"]
        state.activity_state = "hotspot_dwelling"
        state.hotspot_dwell_until = 40.0
        self.runtime.plan_executor.activity_hold_until["visitor"] = 40.0

        self.runtime._maintain_hotspot_activity(39.5)
        self.assertEqual([], self.runtime.plan_executor.releases)

        self.runtime._maintain_hotspot_activity(40.0)
        self.assertEqual(["visitor"], self.runtime.plan_executor.releases)
        self.assertEqual("hotspot_departing", state.activity_state)
        self.assertIsNone(state.hotspot_dwell_until)


@unittest.skipUnless(has_sumo(), "real SUMO binary is required")
class HotspotDwellIntegrationTests(unittest.TestCase):
    def test_real_sumo_dwell_has_no_waiting_stage_or_roadside_jump(self):
        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory) / "hotspot.rou.xml"
            build_hotspot_demand(
                BUND / "bund_ped.rou.xml",
                BUND / "bund.net.xml",
                ROOT / "config" / "crowd_hotspots.json",
                route_path,
                hotspot_id="people_heroes_monument",
                visitor_count=1,
                background_count=0,
            )
            route_tree = ET.parse(route_path)
            route_tree.getroot().find("person").set("depart", "0.00")
            route_tree.write(route_path, encoding="utf-8", xml_declaration=True)
            runtime = SimulationRuntime(
                BUND / "bund.hotspot.sumocfg",
                pedestrian_route_files=[route_path],
                extra_sumo_args=["--route-files", str(route_path)],
                timeline_end_seconds=300.0,
            )
            try:
                runtime.initialize()
                person_id = "hotspot.people_heroes_monument.0000"
                runtime.population.hotspot_release_times.pop(person_id, None)
                runtime.population.hotspot_dwell_seconds[person_id] = 3.0
                runtime.start()
                previous = None
                arrived = None
                saw_waiting_stage = False
                for _ in range(600):
                    step = runtime.tick()
                    current = step.persons.get(person_id)
                    if current is None:
                        continue
                    saw_waiting_stage |= current.stage_type == tc.STAGE_WAITING
                    state = runtime.population.states[person_id]
                    if state.activity_state == "hotspot_dwelling":
                        arrived = current
                        break
                    previous = current
                self.assertIsNotNone(arrived, "visitor never entered runtime-controlled dwell")
                self.assertIsNotNone(previous)
                self.assertFalse(saw_waiting_stage)
                self.assertEqual(tc.STAGE_WALKING, arrived.stage_type)
                arrival_step = ((arrived.x - previous.x) ** 2 + (arrived.y - previous.y) ** 2) ** 0.5
                # A normal 1 s striping-model step can be slightly above
                # 1.2 m; the former roadside stop jump was about 3 m.
                self.assertLess(arrival_step, 1.5)

                held_at = (arrived.x, arrived.y)
                for _ in range(4):
                    held = runtime.tick().persons[person_id]
                    self.assertEqual(tc.STAGE_WALKING, held.stage_type)
                drift = ((held.x - held_at[0]) ** 2 + (held.y - held_at[1]) ** 2) ** 0.5
                self.assertLessEqual(drift, 0.01)

                resumed = None
                for _ in range(10):
                    current = runtime.tick().persons[person_id]
                    if runtime.population.states[person_id].activity_state == "hotspot_departing" and current.speed > 0:
                        resumed = current
                        break
                self.assertIsNotNone(resumed, "visitor did not resume outbound walking")
                displacement = ((resumed.x - held_at[0]) ** 2 + (resumed.y - held_at[1]) ** 2) ** 0.5
                self.assertGreater(displacement, 0.01)
                self.assertLess(displacement, 1.2)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
