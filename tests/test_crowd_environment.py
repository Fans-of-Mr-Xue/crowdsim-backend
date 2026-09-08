from pathlib import Path
import unittest

from crowdsim.environment.crowd_environment import CrowdEnvironment
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot
from crowdsim.infrastructure.network_adapter import ResearchNetwork


ROOT = Path(__file__).resolve().parents[1]
NETWORK = ROOT / "tests" / "scenarios" / "alternative_routes" / "network.net.xml"


def motion(person_id, x, y, edge="entry"):
    return MotionSnapshot(person_id, 1.0, x, y, x, y, 1.0, edge, f"{edge}_0", x, 90.0, 0, 2)


class CrowdEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.environment = CrowdEnvironment(ResearchNetwork(str(NETWORK)))

    def test_neighbours_are_radius_limited_deduplicated_and_topology_isolated(self):
        motions = {"p1": motion("p1", 1, 0), "p2": motion("p2", 3, 0), "p3": motion("p3", 4, 0, "upper_a"), "p4": motion("p4", 30, 0)}
        profiles = {person_id: AgentProfile(person_id, perception_radius=5) for person_id in motions}
        states = {person_id: AgentState(person_id) for person_id in motions}
        observations = self.environment.observe(motions, profiles, states, "s1")
        self.assertEqual(("p2",), observations["p1"].neighbour_ids)
        self.assertEqual(2, observations["p1"].local_people_count)

    def test_objective_density_depends_only_on_real_people_and_explicit_area(self):
        motions = {"p1": motion("p1", 1, 0), "p2": motion("p2", 2, 0)}
        profiles = {person_id: AgentProfile(person_id, perception_radius=5) for person_id in motions}
        base_states = {person_id: AgentState(person_id) for person_id in motions}
        event_states = {person_id: AgentState(person_id, known_events={"alarm": {"content": "urgent"}}) for person_id in motions}
        base = self.environment.observe(motions, profiles, base_states, "s1")
        with_event = self.environment.observe(motions, profiles, event_states, "s2")
        self.assertEqual(base["p1"].objective_density_per_m2, with_event["p1"].objective_density_per_m2)
        self.assertAlmostEqual(2 / base["p1"].local_area_m2, base["p1"].objective_density_per_m2)

    def test_density_classification_has_explicit_units(self):
        self.assertEqual("free", self.environment.classify_density(0.2))
        self.assertEqual("busy", self.environment.classify_density(0.5))
        self.assertEqual("crowded", self.environment.classify_density(1.5))
        self.assertEqual("critical", self.environment.classify_density(3.5))
        self.assertEqual("unknown", self.environment.classify_density(None))


if __name__ == "__main__":
    unittest.main()
