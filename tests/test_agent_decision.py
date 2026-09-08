import asyncio
import unittest

from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot, Observation
from crowdsim.decision.route_provider import RouteCandidate


def context(crowding=0.0, risk=0.0):
    profile = AgentProfile("p", crowding_tolerance=0.4, risk_tolerance=0.4)
    state = AgentState("p", perceived_risk=risk)
    motion = MotionSnapshot("p", 1, 0, 0, 0, 0, 1, "e", "e_0", 0, 0, 0, 2)
    observation = Observation("p", "s1", 1, motion, (), 1, 10, 0.1, crowding)
    return profile, state, observation


class AgentDecisionTests(unittest.TestCase):
    def test_rule_plan_uses_structured_contract(self):
        engine = AgentDecisionEngine()
        profile, state, observation = context(crowding=0.8)
        plan = engine.rule_plan(profile, state, observation)
        self.assertEqual("slow_down", plan.proposed_action)
        self.assertEqual("s1", plan.snapshot_id)
        self.assertIsNotNone(plan.speed_limit)

    def test_rule_reroute_uses_only_candidate(self):
        engine = AgentDecisionEngine()
        profile, state, observation = context(risk=0.9)
        candidate = RouteCandidate("safe", "goal", -1, ("e", "goal"), 12)
        plan = engine.rule_plan(profile, state, observation, [candidate])
        self.assertEqual(candidate.edges, plan.route_edges)

    def test_invalid_llm_target_falls_back_to_rule(self):
        engine = AgentDecisionEngine()
        engine.enabled = True
        engine._request = lambda prompt: {"action": "reroute", "target_id": "hallucinated"}
        profile, state, observation = context()
        candidate = RouteCandidate("safe", "goal", -1, ("e", "goal"), 12)
        plan = asyncio.run(engine.decide(profile, state, observation, [candidate]))
        self.assertEqual("rule_fallback", plan.source)
        self.assertNotEqual("hallucinated", plan.target_id)

    def test_prompt_excludes_background_culture_fields(self):
        engine = AgentDecisionEngine()
        profile, state, observation = context()
        prompt = engine._prompt(profile, state, observation, ())
        self.assertNotIn("nationality", prompt["profile"])
        self.assertNotIn("native_language", prompt["profile"])


if __name__ == "__main__":
    unittest.main()
