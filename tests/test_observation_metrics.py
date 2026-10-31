"""Mathematical and state-transition checks with fabricated snapshots only."""

from dataclasses import replace
import json
import math
from pathlib import Path
import random
from types import SimpleNamespace
import unittest

from traci import constants as tc

from crowdsim.domain.crowdsim_models import AgentState, BehaviorPlan, MotionSnapshot, PlanExecutionResult
from crowdsim.domain.observation_config import IMPLEMENTED_METRIC_IDS, ObservationConfig
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.observation_metrics import ObservationCollector
from crowdsim.infrastructure.sumo_adapter import SumoStepResult


ROOT = Path(__file__).resolve().parents[1]
SQUARE = [(0, 0), (40, 0), (40, 40), (0, 40), (0, 0)]


def person(name, x=10, y=10, speed=1, stage=tc.STAGE_WALKING):
    return MotionSnapshot(name, 0, x, y, x, y, speed, "road", "road_0", 0, 0, 0, stage)


def snapshot(time, *people, arrived=()):
    return SumoStepResult(time, {item.person_id: item for item in people}, {}, (), arrived)


def measure(collector, step, states=None, density=0.1):
    states = states or {name: AgentState(name) for name in step.persons}
    return collector.measure(step, states,
        {name: {"density_person_per_m2": density} for name in step.persons}, f"test:{step.time_seconds}")


class ObservationMetricsTests(unittest.TestCase):
    def collector(self, shape=SQUARE, ids=IMPLEMENTED_METRIC_IDS, **config):
        return ObservationCollector(shape, ids, ObservationConfig(**config))

    def test_region_membership_speed_and_density_are_not_whole_network(self):
        collector = self.collector()
        result = measure(collector, snapshot(0, person("a", speed=2), person("b", 30, 10, 0), person("outside", 50, 10, 9)))
        row = result["global"]
        self.assertEqual(2, row["person_count"])
        self.assertEqual(3, row["network_person_count"])
        self.assertEqual(1, row["outside_scope_person_count"])
        self.assertEqual(2 / 1600, row["density_person_per_m2"])
        self.assertEqual(1, row["avg_speed_mps"])
        self.assertEqual(2, row["moving_avg_speed_mps"])
        self.assertEqual(2, sum(cell["person_count"] for cell in result["cells"]))
        self.assertEqual(1600, sum(cell["area_m2"] for cell in result["cells"]))
        self.assertEqual(2, sum(cell["density_person_per_m2"] * cell["area_m2"] for cell in result["cells"]))

    def test_empty_cells_and_empty_region_distinguish_density_zero_from_speed_unknown(self):
        result = measure(self.collector(), snapshot(0))
        self.assertEqual(0, result["global"]["density_person_per_m2"])
        self.assertIsNone(result["global"]["avg_speed_mps"])
        self.assertEqual(4, len(result["cells"]))
        self.assertTrue(all(row["density_person_per_m2"] == 0 and row["avg_speed_mps"] is None for row in result["cells"]))
        self.assertTrue(all(row["difference_person_per_m2"] == 0 for row in result["boundaries"] if row["status"] == "valid"))

    def test_grid_line_and_scope_boundary_people_have_exactly_one_owner(self):
        result = measure(self.collector(), snapshot(0,
            person("center", 20, 20), person("east", 40, 10), person("corner", 40, 40), person("west", 0, 10)))
        self.assertEqual(4, sum(row["person_count"] for row in result["cells"]))
        self.assertEqual("cell-r0001-c0001", result["agents"]["center"]["cell_id"])
        self.assertEqual("cell-r0001-c0001", result["agents"]["corner"]["cell_id"])

    def test_concave_clipped_grid_conserves_area_and_population(self):
        shape = [(3, 3), (37, 3), (37, 14), (14, 14), (14, 37), (3, 37), (3, 3)]
        collector = self.collector(shape)
        rng = random.Random(7)
        people = [person(str(index), rng.uniform(0, 40), rng.uniform(0, 40)) for index in range(200)]
        result = measure(collector, snapshot(0, *people))
        self.assertAlmostEqual(11 * 34 + 11 * 23, collector.scope.area)
        self.assertAlmostEqual(collector.scope.area, sum(row["area_m2"] for row in result["cells"]))
        self.assertEqual(result["global"]["person_count"], sum(row["person_count"] for row in result["cells"]))

    def test_shared_boundary_uses_two_bands_and_is_not_duplicated(self):
        result = measure(self.collector(), snapshot(0,
            person("left1", 18, 10), person("left2", 19, 10), person("right", 22, 10), person("far", 5, 10)))
        rows = [row for row in result["boundaries"] if row["cell_a"] == "cell-r0000-c0000" and row["cell_b"] == "cell-r0000-c0001"]
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(100, row["area_a_m2"])
        self.assertEqual(100, row["area_b_m2"])
        self.assertEqual(2, row["person_count_a"])
        self.assertEqual(1, row["person_count_b"])
        self.assertAlmostEqual(0.01, row["difference_person_per_m2"])
        self.assertAlmostEqual(0.01, row["absolute_difference_person_per_m2"])
        self.assertTrue(all(row["difference_person_per_m2"] is None and row["density_b_person_per_m2"] is None
                            for row in result["boundaries"] if row["cell_b"] is None))

    def test_boundary_line_person_is_not_counted_on_both_sides(self):
        result = measure(self.collector(), snapshot(0, person("line", 20, 10)))
        row = next(row for row in result["boundaries"] if row["cell_a"] == "cell-r0000-c0000" and row["cell_b"] == "cell-r0000-c0001")
        self.assertEqual((0, 1), (row["person_count_a"], row["person_count_b"]))

    def test_boundary_only_selection_computes_local_density_dependency(self):
        collector = self.collector(ids=("boundary-density-difference",))
        result = measure(collector, snapshot(0, person("a")))
        self.assertIn("local-density", collector.computed_ids)
        self.assertIsNone(result["global"]["density_person_per_m2"])
        self.assertIsNone(result["global"]["avg_speed_mps"])
        self.assertIsNone(result["agents"]["a"]["psychological_state"])
        self.assertTrue(result["boundaries"])

    def test_invalid_positions_and_speeds_do_not_become_zero_measurements(self):
        result = measure(self.collector(), snapshot(0, person("badxy", math.nan, 10), person("badspeed", speed=math.nan), person("good", speed=2)))
        self.assertEqual(1, result["global"]["invalid_position_count"])
        self.assertEqual(2, result["global"]["person_count"])
        self.assertEqual(1, result["global"]["invalid_speed_count"])
        self.assertEqual(2, result["global"]["avg_speed_mps"])
        json.dumps(result, allow_nan=False)

    def test_behavior_and_crowding_are_independent_and_do_not_modify_agent_state(self):
        collector = self.collector()
        state = AgentState("a")
        first = measure(collector, snapshot(0, person("a")), {"a": state})
        before = state.__dict__.copy()
        second = measure(collector, snapshot(1, person("a", speed=0, stage=tc.STAGE_WAITING)), {"a": state}, density=2)
        self.assertEqual("walking", first["agents"]["a"]["behavior_state"])
        self.assertEqual("waiting", second["agents"]["a"]["behavior_state"])
        self.assertTrue(second["agents"]["a"]["crowded"])
        self.assertEqual(before, state.__dict__)
        self.assertTrue(any(row["from_state"] == "walking" and row["to_state"] == "waiting" for row in second["transitions"]))
        self.assertEqual(1, second["global"]["behavior_waiting_count"])
        state.blocked_duration = 6
        blocked = measure(collector, snapshot(2, person("a", speed=0)), {"a": state})
        self.assertEqual("blocked", blocked["agents"]["a"]["behavior_state"])

    def test_only_successful_risk_reroute_is_avoidance_and_it_expires(self):
        collector = self.collector()
        state = AgentState("a", perceived_risk=0.8)
        plan = BehaviorPlan("a", "test:0", "reroute")
        collector.record_execution(plan, PlanExecutionResult("a", "reroute", "continue", "rejected", 0), state)
        self.assertEqual("walking", measure(collector, snapshot(0, person("a")), {"a": state})["agents"]["a"]["behavior_state"])
        collector.record_execution(plan, PlanExecutionResult("a", "reroute", "reroute", "applied", 0), state)
        self.assertEqual("avoiding", measure(collector, snapshot(1, person("a")), {"a": state})["agents"]["a"]["behavior_state"])
        self.assertEqual("walking", measure(collector, snapshot(5, person("a")), {"a": state})["agents"]["a"]["behavior_state"])
        collector.record_execution(plan, PlanExecutionResult("a", "reroute", "reroute", "applied", 6), AgentState("a", perceived_risk=0.1))
        self.assertEqual("walking", measure(collector, snapshot(6, person("a")), {"a": state})["agents"]["a"]["behavior_state"])

    def test_psychology_confirmation_hysteresis_and_unknown_raw_values(self):
        collector = self.collector()
        state = AgentState("a", stress=0.2)
        self.assertEqual("calm", measure(collector, snapshot(0, person("a")), {"a": state})["agents"]["a"]["psychological_state"])
        state.stress = 0.9
        for time in (1, 5):
            self.assertEqual("calm", measure(collector, snapshot(time, person("a")), {"a": state})["agents"]["a"]["psychological_state"])
        result = measure(collector, snapshot(6, person("a")), {"a": state})
        self.assertEqual("panic", result["agents"]["a"]["psychological_state"])
        self.assertTrue(any(row["dimension"] == "psychological_state" and row["to_state"] == "panic" for row in result["transitions"]))
        state.stress = 0.68
        self.assertEqual("panic", measure(collector, snapshot(7, person("a")), {"a": state})["agents"]["a"]["psychological_state"])
        state.stress = math.nan
        result = measure(collector, snapshot(8, person("a")), {"a": state})
        self.assertEqual("unknown", result["agents"]["a"]["psychological_state"])
        self.assertIsNone(result["agents"]["a"]["stress"])
        json.dumps(result, allow_nan=False)

    def test_region_exit_and_arrival_are_recorded_without_inventing_behavior(self):
        collector = self.collector()
        measure(collector, snapshot(0, person("a")))
        result = measure(collector, snapshot(1, person("a", 50, 10)))
        self.assertEqual(0, result["global"]["person_count"])
        self.assertTrue(any(row["reason"] == "scope_exit" for row in result["transitions"]))
        measure(collector, snapshot(2, person("a")))
        result = measure(collector, snapshot(3, arrived=("a",)))
        self.assertTrue(any(row["reason"] == "arrived" and row["to_state"] == "absent" for row in result["transitions"]))
        self.assertEqual({}, collector.previous_agents)

    def test_geometry_and_configuration_validation(self):
        for shape in ([(0, 0), (1, 1), (2, 2)], [(0, 0), (40, 40), (40, 0), (0, 40)]):
            with self.assertRaises(ValueError):
                self.collector(shape)
        for changes in ({"grid_size_m": 0}, {"stress_panic_threshold": 0.3}, {"stress_hysteresis": 0.3},
                        {"max_grid_cells": 1.5}, {"boundary_band_width_m": 21}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ObservationConfig(**changes)
        with self.assertRaisesRegex(ValueError, "max_grid_cells"):
            self.collector(max_grid_cells=1)
        collector = self.collector()
        measure(collector, snapshot(0))
        with self.assertRaisesRegex(ValueError, "increasing"):
            measure(collector, snapshot(0))

    def test_global_only_observation_does_not_require_a_large_grid(self):
        collector = self.collector(ids=("global-density", "global-speed"), max_grid_cells=1)
        result = measure(collector, snapshot(0, person("a")))
        self.assertEqual(1 / 1600, result["global"]["density_person_per_m2"])
        self.assertEqual([], result["cells"])
        self.assertEqual([], result["boundaries"])

    def test_psychology_candidate_resets_after_short_fluctuation(self):
        collector = self.collector()
        state = AgentState("a", stress=0.2)
        measure(collector, snapshot(0, person("a")), {"a": state})
        state.stress = 0.9
        measure(collector, snapshot(1, person("a")), {"a": state})
        state.stress = 0.2
        measure(collector, snapshot(4, person("a")), {"a": state})
        state.stress = 0.9
        self.assertEqual("calm", measure(collector, snapshot(6, person("a")), {"a": state})["agents"]["a"]["psychological_state"])
        self.assertEqual("calm", measure(collector, snapshot(10, person("a")), {"a": state})["agents"]["a"]["psychological_state"])
        self.assertEqual("panic", measure(collector, snapshot(11, person("a")), {"a": state})["agents"]["a"]["psychological_state"])

    def test_real_network_projection_and_new_m3_geometry_without_starting_sumo(self):
        net = ResearchNetwork(str(ROOT / "scenarios/shanghai_bund/bund.net.xml"))
        junction = net.net.getNode("monument_m3_ring_junction").getCoord()
        lon, lat = net.xy_to_lonlat(*junction)
        x, y = net.lonlat_to_xy_strict(lon, lat)
        self.assertAlmostEqual(junction[0], x, places=5)
        self.assertAlmostEqual(junction[1], y, places=5)
        scope = [(x - 20, y - 20), (x + 20, y - 20), (x + 20, y + 20), (x - 20, y + 20)]
        result = measure(self.collector(scope), snapshot(0, replace(person("m3", x, y), edge_id="monument_m3")))
        self.assertEqual(1, result["global"]["person_count"])
        self.assertIsNotNone(result["agents"]["m3"]["cell_id"])
        unprojected = ResearchNetwork(str(ROOT / "tests/scenarios/unidirectional_corridor/network.net.xml"))
        with self.assertRaisesRegex(ValueError, "projected"):
            unprojected.lonlat_to_xy_strict(121, 31)
        for projected, factor in ((False, 1.0), (True, 0.3048)):
            network = ResearchNetwork.__new__(ResearchNetwork)
            network.net = SimpleNamespace(getGeoProj=lambda: SimpleNamespace(crs=SimpleNamespace(
                is_projected=projected, axis_info=[SimpleNamespace(unit_conversion_factor=factor)])))
            with self.assertRaisesRegex(ValueError, "metre-based"):
                network.lonlat_to_xy_strict(121, 31)


if __name__ == "__main__":
    unittest.main()
