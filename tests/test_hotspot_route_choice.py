import unittest

from traci import constants as tc

from crowdsim.decision.hotspot_route_choice import HotspotRouteChoice
from crowdsim.decision.route_provider import RouteCandidate
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot


HOTSPOT = {
    "id": "monument",
    "target_edge": "ring",
    "target_position": 17.5,
    "target_edges": ("ring",),
    "entry_edges": ("north", "south"),
    "park_access_edges": ("park_a", "park_b"),
    "excluded_edges": ("underground",),
    "route_choice": {
        "enabled": True,
        "decision_interval_seconds": 10.0,
        "switch_cooldown_seconds": 15.0,
        "minimum_savings_seconds": 20.0,
        "free_flow_density_person_per_m2": 0.5,
        "congested_density_person_per_m2": 1.5,
        "speed_floor_mps": 0.2,
    },
}


def motion(edge="park_a", now=30.0):
    return MotionSnapshot("p", now, 0, 0, 0, 0, 1, edge, f"{edge}_0", 3, 0, 0, tc.STAGE_WALKING)


class Provider:
    edge_lengths = {"park_a": 30.0, "park_b": 30.0, "north": 20.0, "south": 20.0, "ring": 50.0}

    def __init__(self):
        self.arrival_positions = []

    def build_via_candidate(
        self,
        snapshot,
        *,
        target_id,
        target_edge,
        via_edge,
        arrival_position=None,
        edge_costs=None,
        default_speed_mps=1.35,
        **kwargs,
    ):
        self.arrival_positions.append(arrival_position)
        base = 20.0 if via_edge == "north" else 40.0
        dynamic_edge_time = (edge_costs or {}).get(via_edge)
        delay = 0.0
        if dynamic_edge_time is not None:
            delay = max(0.0, dynamic_edge_time - self.edge_lengths[via_edge] / default_speed_mps)
        return RouteCandidate(
            target_id, target_edge, 49.0, (snapshot.edge_id, via_edge, target_edge), base + delay,
            target_kind="hotspot_route", entry_edge=via_edge, base_cost_seconds=base,
            congestion_delay_seconds=delay,
        )


class HotspotRouteChoiceTests(unittest.TestCase):
    def setUp(self):
        self.provider = Provider()
        self.choice = HotspotRouteChoice(None, self.provider)

    def test_congestion_delay_can_make_longer_free_flow_entrance_faster(self):
        metrics = {
            "north": {"person_count": 20, "density_person_per_m2": 2.0, "avg_speed_mps": 0.2},
            "south": {"person_count": 0, "density_person_per_m2": 0.0, "avg_speed_mps": 0.0},
        }

        candidates = self.choice.build_candidates(
            motion(), AgentProfile("p"), AgentState("p"), HOTSPOT, metrics
        )

        self.assertEqual(("south", "north"), tuple(item.entry_edge for item in candidates))
        north = next(item for item in candidates if item.entry_edge == "north")
        self.assertGreater(north.congestion_delay_seconds, 0)
        self.assertGreater(north.estimated_cost_seconds, 40)
        self.assertEqual([17.5, 17.5], self.provider.arrival_positions)

    def test_missing_person_target_position_is_rejected(self):
        hotspot = {key: value for key, value in HOTSPOT.items() if key != "target_position"}

        with self.assertRaisesRegex(ValueError, "missing target_position"):
            self.choice.build_candidates(
                motion(), AgentProfile("p"), AgentState("p"), hotspot, {}
            )

    def test_selected_route_is_only_reconsidered_inside_park(self):
        state = AgentState("p", hotspot_entry_edge="north")

        self.assertEqual((), self.choice.build_candidates(
            motion(edge="outside"), AgentProfile("p"), state, HOTSPOT, {}
        ))
        self.assertEqual(2, len(self.choice.build_candidates(
            motion(edge="park_a"), AgentProfile("p"), state, HOTSPOT, {}
        )))

    def test_entry_edge_is_a_commitment_zone(self):
        candidates = self.choice.build_candidates(
            motion(edge="north"), AgentProfile("p"), AgentState("p"), HOTSPOT, {}
        )

        self.assertEqual((), candidates)


if __name__ == "__main__":
    unittest.main()
