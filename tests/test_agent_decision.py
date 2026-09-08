import asyncio
import unittest

from agent_decision import AgentDecisionEngine
from crowdsim_models import Agent


def make_agent(**overrides):
    values = {
        "id": "p1",
        "kind": "pedestrian",
        "route": ["edge"],
        "route_index": 0,
        "distance": 0.0,
        "base_speed": 1.2,
        "speed": 1.2,
        "lateral": 0.0,
    }
    values.update(overrides)
    return Agent(**values)


class AgentDecisionTests(unittest.TestCase):
    def test_personal_threshold_triggers_slow_down(self):
        engine = AgentDecisionEngine()
        engine.enabled = False
        agent = make_agent(nearby_people=12, acceptable_people=8, density_level="crowded")
        action, reason, source = asyncio.run(engine.decide(agent))
        self.assertEqual("slow_down", action)
        self.assertIn("threshold", reason)
        self.assertEqual("local", source)

    def test_critical_density_triggers_avoidance(self):
        engine = AgentDecisionEngine()
        engine.enabled = False
        action, _, _ = asyncio.run(engine.decide(make_agent(density_level="critical")))
        self.assertEqual("avoid", action)


if __name__ == "__main__":
    unittest.main()
