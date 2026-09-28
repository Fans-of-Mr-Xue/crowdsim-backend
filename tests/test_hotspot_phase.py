import unittest

from crowdsim.infrastructure.hotspot_phase import HotspotPhaseTracker


class HotspotPhaseTrackerTests(unittest.TestCase):
    def test_emits_ordered_measurement_driven_process(self):
        tracker = HotspotPhaseTracker({"confirmation_seconds": 5, "trend_window_seconds": 20})
        samples = [
            (0, 5, 0.02),
            (10, 70, 0.20),
            (15, 100, 0.35),
            (20, 420, 1.55),
            (25, 500, 1.65),
            (30, 510, 1.67),
            (40, 470, 1.45),
            (45, 430, 1.25),
            (50, 8, 0.02),
            (55, 5, 0.01),
            (60, 4, 0.01),
        ]

        states = [tracker.update(*sample) for sample in samples]

        self.assertEqual("normal", states[0]["phase"])
        self.assertEqual("building", states[2]["phase"])
        self.assertEqual("congested", states[4]["phase"])
        self.assertEqual("dispersing", states[8]["phase"])
        self.assertEqual("cleared", states[10]["phase"])
        self.assertEqual(
            ["normal", "building", "congested", "dispersing", "cleared"],
            [item["phase"] for item in states[-1]["transitions"]],
        )

    def test_phase_does_not_regress_after_clearance(self):
        tracker = HotspotPhaseTracker({"confirmation_seconds": 0})
        for sample in ((0, 60, 0.2), (1, 60, 0.2), (2, 500, 1.6), (3, 500, 1.6), (4, 400, 1.2), (5, 300, 1.0), (6, 5, 0.01), (7, 5, 0.01)):
            state = tracker.update(*sample)
        state = tracker.update(8, 80, 0.3)

        self.assertEqual("cleared", state["phase"])

    def test_residual_congestion_prevents_false_clearance(self):
        tracker = HotspotPhaseTracker({
            "confirmation_seconds": 0,
            "congested_core_density_person_per_m2": 0.6,
            "residual_backlog_person_count": 20,
            "cleared_remaining_visitor_count": 10,
            "cleared_blocked_person_count": 5,
        })
        tracker.update(0, 60, 0.2, remaining_visitor_count=100, blocked_person_count=20)
        tracker.update(10, 100, 0.7, remaining_visitor_count=100, blocked_person_count=30)
        tracker.update(20, 30, 0.1, remaining_visitor_count=80, blocked_person_count=25)
        residual = tracker.update(30, 0, 0, remaining_visitor_count=60, blocked_person_count=20)

        self.assertEqual("residual_congestion", residual["phase"])
        self.assertIn("60名访客未完成", residual["reason"])
        self.assertEqual(
            ["normal", "building", "congested", "dispersing", "residual_congestion"],
            [item["phase"] for item in residual["transitions"]],
        )

        cleared = tracker.update(40, 0, 0, remaining_visitor_count=5, blocked_person_count=2)
        self.assertEqual("cleared", cleared["phase"])


if __name__ == "__main__":
    unittest.main()
