import unittest
from pathlib import Path

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary
from pedestrian_decision_skill import PedestrianDecisionSkill


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "tests" / "scenarios" / "unidirectional_corridor"


def has_sumo() -> bool:
    try:
        discover_sumo_binary()
        return True
    except FileNotFoundError:
        return False


class StubDecisionClient:
    config = {"model": "stub-deepseek"}

    async def complete(self, messages):
        return '{"action":"continue","target_id":null,"reason":"状态稳定","confidence":0.88}'


@unittest.skipUnless(has_sumo(), "real SUMO binary is required")
class SkillRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_plan_is_scheduled_applied_and_serialized(self):
        skill = PedestrianDecisionSkill(client=StubDecisionClient())
        runtime = SimulationRuntime(
            SCENARIO / "scenario.sumocfg",
            pedestrian_route_files=[SCENARIO / "demand.rou.xml"],
            decision_engine=skill,
            use_llm=True,
        )
        try:
            runtime.initialize()
            runtime.start()
            await runtime.tick_async()  # establishes the first populated observation
            if runtime.state == RuntimeState.RUNNING:
                await runtime.tick_async()  # consumes that frozen observation
            plans = [state.current_plan for state in runtime.population.states.values() if state.current_plan]
            self.assertTrue(plans)
            self.assertEqual("llm", plans[0].source)
            self.assertEqual(0.88, plans[0].confidence)
            pedestrian = runtime.frame()["pedestrians"][0]
            self.assertIn("decision_confidence", pedestrian["state"])
            self.assertEqual(0.88, pedestrian["state"]["decision_confidence"])
            self.assertIn(pedestrian["state"]["nationality"], {"unspecified", "domestic", "international"})
            diagnostics = runtime.diagnostics()["decision_engine"]
            self.assertEqual(runtime.decision_diagnostics, diagnostics)
            self.assertTrue(diagnostics["enabled"])
            self.assertEqual("stub-deepseek", diagnostics["model"])
            self.assertEqual(1, diagnostics["llm_decisions"])
            self.assertEqual(0, diagnostics["fallback_decisions"])
            self.assertFalse(diagnostics["last_error"])
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
