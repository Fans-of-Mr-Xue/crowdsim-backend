import unittest

from crowdsim.environment.activity_planner import ActivityPlanner
from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot, Observation
from crowdsim.decision.route_provider import RouteCandidate


class FakeCatalog:
    pois = {
        "museum": {"id": "museum", "kind": "activity", "open": [0, 100]},
        "park": {"id": "park", "kind": "activity", "open": [0, 100]},
        "exit": {"id": "exit", "kind": "exit", "open": [0, 100]},
    }

    def available(self, now, known_ids=None):
        return [item for item in self.pois.values() if item["id"] in set(known_ids or self.pois)]


class ActivityPlannerTests(unittest.TestCase):
    def test_visit_purpose_and_familiarity_initialize_known_goals(self):
        planner = ActivityPlanner(FakeCatalog())
        tourist = AgentState("tourist")
        commuter = AgentState("commuter")
        planner.initialize(AgentProfile("tourist", familiarity=1, visit_purpose="tourism"), tourist)
        planner.initialize(AgentProfile("commuter", familiarity=1, visit_purpose="commute"), commuter)
        self.assertEqual(3, len(tourist.activity_plan))
        self.assertEqual(["exit"], commuter.activity_plan)
        self.assertEqual(tourist.activity_plan[0], tourist.current_goal)

    def test_activity_candidate_becomes_executable_change_goal(self):
        profile = AgentProfile("p", visit_purpose="tourism")
        state = AgentState("p", current_goal="museum")
        motion = MotionSnapshot("p", 1, 0, 0, 0, 0, 1, "e", "e_0", 0, 0, 0, 2)
        observation = Observation("p", "s", 1, motion)
        candidate = RouteCandidate("museum", "m", -1, ("e", "m"), 10, "activity", 60, ("exit",))
        plan = AgentDecisionEngine().rule_plan(profile, state, observation, (candidate,))
        self.assertEqual("change_goal", plan.proposed_action)
        self.assertEqual(60, plan.activity_duration)
        self.assertEqual(("exit",), plan.next_route_edges)


if __name__ == "__main__":
    unittest.main()
