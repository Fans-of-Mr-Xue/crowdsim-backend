import asyncio
import unittest

from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot, Observation
from crowdsim.decision.decision_scheduler import DecisionScheduler


def inputs(count):
    profiles, states, observations = {}, {}, {}
    for index in range(count):
        person_id = f"p{index:02d}"
        profiles[person_id] = AgentProfile(person_id)
        states[person_id] = AgentState(person_id)
        motion = MotionSnapshot(person_id, 1, index, 0, index, 0, 1, "e", "e_0", index, 0, 0, 2)
        observations[person_id] = Observation(person_id, "s", 1, motion)
    return profiles, states, observations


class DecisionSchedulerTests(unittest.TestCase):
    def test_rule_mode_processes_all_thirty_due_people(self):
        engine = AgentDecisionEngine()
        scheduler = DecisionScheduler(engine)
        profiles, states, observations = inputs(30)
        due = scheduler.collect_due(states, observations, 1)
        plans = scheduler.resolve_rule(due, profiles, states, observations)
        self.assertEqual(30, len(plans))

    def test_llm_budget_has_explicit_rule_fallback(self):
        engine = AgentDecisionEngine()
        engine.enabled = True
        engine._request = lambda prompt: {"action": "continue", "reason": "ok"}
        scheduler = DecisionScheduler(engine, llm_budget=5, concurrency=2)
        profiles, states, observations = inputs(12)
        plans = asyncio.run(scheduler.resolve(list(observations), profiles, states, observations, use_llm=True))
        self.assertEqual(12, len(plans))
        self.assertEqual(7, scheduler.budget_fallback_count)
        self.assertEqual(5, sum(plan.source == "llm" for plan in plans))


if __name__ == "__main__":
    unittest.main()
