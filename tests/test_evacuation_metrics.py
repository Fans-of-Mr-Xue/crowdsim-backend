"""Known density distributions and population ledgers, without starting SUMO."""

import math
import unittest

from crowdsim.core.population_manager import PopulationLedger
from crowdsim.domain.observation_config import EVACUATION_METRIC_IDS
from tests.test_observation_metrics import SQUARE, measure, person, snapshot
from crowdsim.infrastructure.observation_metrics import ObservationCollector


class EvacuationMetricsTests(unittest.TestCase):
    def setUp(self):
        self.collector = ObservationCollector(SQUARE, EVACUATION_METRIC_IDS)
        self.ledger = PopulationLedger(planned_ids={"a", "b"}, departed_ids={"a", "b"}, active_ids={"a", "b"})
        self.sample(.5, person("a"), person("b", 30, 10))
        self.tracker = self.collector.evacuation

    def sample(self, time, *people, arrived=()):
        self.ledger.active_ids = {p.person_id for p in people}
        self.ledger.departed_ids.update(self.ledger.active_ids | set(arrived))
        self.ledger.arrived_ids.update(arrived)
        from crowdsim.domain.crowdsim_models import AgentState
        step = snapshot(time, *people, arrived=arrived)
        return self.collector.measure(step, {key: AgentState(key) for key in step.persons}, {},
                                      f"test:{time}", ledger=self.ledger)

    def policy(self, name="police_guidance", request="first"):
        self.tracker.policy_applied({"name": name, "applied_at": self.tracker.latest["time_seconds"], "physical_change": False},
                                    {"request_id": request})

    def test_time_and_efficiency_use_application_density_and_simulation_clock(self):
        self.tracker.start_event(.5, self.ledger)
        self.sample(1.5, person("b", 30, 10), arrived=("a",))
        self.policy()
        # Reattaching/resuming cannot move either baseline to a later time.
        self.assertFalse(self.tracker.start_event(1.5, self.ledger))
        self.policy("temporary_diversion", "second")
        self.sample(2.5, arrived=("b",))
        result = self.tracker.summary()
        self.assertEqual((.5, 1.5, 2.5), (result["event_start_time_seconds"], result["strategy_applied_time_seconds"], result["completion_time_seconds"]))
        self.assertEqual(1, result["metrics"]["evacuation-time"]["value"])
        self.assertEqual(2, result["total_event_time_seconds"])
        self.assertAlmostEqual(1 / 1600, result["metrics"]["evacuation-efficiency"]["value"])
        self.assertEqual("first", result["first_evacuation_policy"]["request_id"])
        self.assertFalse(result["first_evacuation_policy"]["physical_change"])
        self.assertFalse(self.tracker.policy_applications[1]["starts_evacuation"])

    def test_density_difference_observes_redistribution_with_constant_total_density(self):
        self.tracker.start_event(.5, self.ledger)
        moved = self.sample(1.5, person("a"), person("b"))
        self.assertEqual(self.tracker.initial["density_person_per_m2"], moved["global"]["density_person_per_m2"])
        differences = {row["cell_id"]: row["absolute_initial_difference_person_per_m2"] for row in moved["cells"]}
        self.assertAlmostEqual(1 / 400, differences["cell-r0000-c0000"])
        self.assertAlmostEqual(1 / 400, differences["cell-r0000-c0001"])
        self.assertEqual(0, differences["cell-r0001-c0000"])

    def test_empty_snapshot_does_not_finish_with_pending_departures(self):
        self.ledger.planned_ids.add("c")
        self.tracker.start_event(.5, self.ledger)
        self.policy()
        self.sample(1.5, arrived=("a", "b"))
        self.assertIsNone(self.tracker.final)
        self.assertEqual(1, self.tracker.progress["pending_person_count"])
        self.sample(2.5, person("c"))
        self.sample(3.5, arrived=("c",))
        self.assertEqual("complete", self.tracker.summary()["status"])

    def test_abnormal_removal_disappearance_and_new_cohort_cannot_count_as_completed(self):
        for kind in ("removed", "unknown", "unexpected"):
            with self.subTest(kind=kind):
                self.setUp()
                self.tracker.start_event(.5, self.ledger)
                self.policy()
                if kind == "removed":
                    self.ledger.explicitly_removed["b"] = "manual"
                    self.sample(1.5, arrived=("a",))
                elif kind == "unknown":
                    self.ledger.unknown_disappearances.add("b")
                    self.sample(1.5, arrived=("a", "b"))
                else:
                    self.ledger.planned_ids.add("c")
                    self.sample(1.5, arrived=("a", "b"))
                result = self.tracker.summary("complete", "no_expected_entities")
                self.assertEqual("invalid_population_accounting", result["status"])
                self.assertIsNone(result["completion_time_seconds"])
                self.assertIsNone(result["metrics"]["evacuation-time"]["value"])

    def test_time_limit_interruption_and_error_leave_final_values_unset(self):
        self.tracker.start_event(.5, self.ledger)
        self.policy()
        self.sample(3.5, person("a"), person("b"))
        for run_status, reason, expected in (("complete", "timeline_end", "incomplete"),
                                              ("interrupted", "runtime_closed", "interrupted"),
                                              ("error", "runtime_error", "error")):
            result = self.tracker.summary(run_status, reason)
            self.assertEqual(expected, result["status"])
            self.assertEqual(3, result["elapsed_since_strategy_seconds"])
            self.assertIsNone(result["metrics"]["evacuation-time"]["value"])
            self.assertIsNone(result["metrics"]["evacuation-efficiency"]["value"])
            self.assertIsNone(result["completion_time_seconds"])

    def test_observe_only_and_no_policy_do_not_start_evacuation(self):
        self.tracker.start_event(.5, self.ledger)
        self.policy("observe_only")
        self.sample(1.5, arrived=("a", "b"))
        metrics = self.tracker.summary()["metrics"]
        self.assertEqual("not_started", metrics["evacuation-time"]["status"])
        self.assertEqual("not_started", metrics["evacuation-efficiency"]["status"])
        self.assertEqual("complete", metrics["absolute-evacuation-density"]["status"])
        self.assertFalse(self.tracker.policy_applications[0]["starts_evacuation"])

    def test_invalid_position_baseline_does_not_fabricate_efficiency_or_cell_differences(self):
        self.sample(1.5, person("a", math.nan), person("b"))
        self.tracker.start_event(1.5, self.ledger)
        self.policy()
        final = self.sample(2.5, arrived=("a", "b"))
        metrics = self.tracker.summary()["metrics"]
        self.assertEqual("complete", metrics["evacuation-time"]["status"])
        self.assertEqual("invalid_density_baseline", metrics["evacuation-efficiency"]["status"])
        self.assertIsNone(metrics["evacuation-efficiency"]["value"])
        self.assertTrue(all(row["absolute_initial_difference_person_per_m2"] is None for row in final["cells"]))

    def test_independent_selection_computes_only_required_density_dependencies(self):
        for name, cells, density in (("evacuation-time", False, False),
                                    ("evacuation-efficiency", False, True),
                                    ("absolute-evacuation-density", True, True)):
            collector = ObservationCollector(SQUARE, [name])
            sample = measure(collector, snapshot(.5, person("a")))
            self.assertEqual(cells, bool(sample["cells"]))
            self.assertEqual(density, sample["global"]["density_person_per_m2"] is not None)
            self.assertEqual([name], sample["metric_ids"])

    def test_zero_population_is_not_a_successful_evacuation(self):
        collector = ObservationCollector(SQUARE, EVACUATION_METRIC_IDS)
        measure(collector, snapshot(.5))
        collector.evacuation.start_event(.5, PopulationLedger())
        measure(collector, snapshot(1.5))
        self.assertEqual("no_target_population", collector.evacuation.summary()["status"])
        self.assertIsNone(collector.evacuation.final)


if __name__ == "__main__":
    unittest.main()
