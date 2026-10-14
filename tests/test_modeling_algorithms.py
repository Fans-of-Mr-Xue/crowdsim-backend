import asyncio
from pathlib import Path
import unittest

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.decision.route_provider import RouteCandidate
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot, Observation
from crowdsim.modeling_algorithm.contracts import Experience, PADState
from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
from crowdsim.modeling_algorithm.individual_model.desire_goal.emotion_driven_desire import EmotionDrivenDesireModel
from crowdsim.modeling_algorithm.individual_model.emotion.pad_continuous import PADContinuousEmotionModel
from crowdsim.modeling_algorithm.individual_model.game.bounded_rationality import BoundedRationalGameModel
from crowdsim.modeling_algorithm.individual_model.learning.experience_replay import ExperienceReplayBuffer
from crowdsim.modeling_algorithm.individual_model.pipeline import IndividualModelingEngine
from crowdsim.modeling_algorithm.registry import algorithm_catalog, method_status


def context(*, role="visitor", risk=0.7, crowding=0.8, stress=0.5, fatigue=0.2):
    profile = AgentProfile(
        "p1", crowd_role=role, free_walking_speed=1.4, mobility=0.9,
        risk_tolerance=0.4, crowding_tolerance=0.4,
        familiarity=0.5, authority_compliance=0.8,
        group_cohesion=0.6, endurance=0.7,
    )
    state = AgentState("p1", perceived_risk=risk, perceived_crowding=crowding,
                       stress=stress, fatigue=fatigue, blocked_duration=8)
    motion = MotionSnapshot("p1", 10, 0, 0, 0, 0, 0.4, "e", "e_0", 0, 0, 0, 2)
    observation = Observation(
        "p1", "snapshot-1", 10, motion,
        local_people_count=10, local_area_m2=10,
        objective_density_per_m2=1.0,
        perceived_crowding=crowding, perceived_risk=risk,
    )
    return profile, state, observation


def implemented_architecture():
    return [{
        "id": "visitor",
        "capabilities": {"emotion": True, "game": True, "learning": True},
        "architecture": [
            {"id": "perception", "selected": True, "method": "局部邻域观测"},
            {"id": "emotion", "selected": True, "method": "PAD 连续情感模型"},
            {"id": "desire", "selected": True, "method": "情感驱动欲望"},
            {"id": "goal", "selected": True, "method": "有限状态目标"},
            {"id": "game", "selected": True, "method": "有限理性博弈"},
            {"id": "decision", "selected": True, "method": "LLM-driven"},
            {"id": "action", "selected": True, "method": "SUMO striping"},
            {"id": "learning", "selected": True, "method": "经验回放"},
        ],
    }]


class ModelingAlgorithmTests(unittest.TestCase):
    def test_registry_distinguishes_implemented_and_reserved_methods(self):
        catalog = algorithm_catalog()
        self.assertGreater(len(catalog), 60)
        self.assertTrue(method_status("PAD 连续情感模型")["implemented"])
        self.assertTrue(method_status("LLM-driven")["implemented"])
        self.assertFalse(method_status("OCC 事件评价模型")["implemented"])

    def test_pad_is_bounded_and_reacts_to_high_risk(self):
        profile, state, observation = context()
        emotion = PADContinuousEmotionModel(inertia=0).update(profile, state, observation)
        self.assertGreaterEqual(emotion.arousal, 0)
        self.assertLessEqual(emotion.arousal, 1)
        self.assertGreater(emotion.arousal, 0.5)
        self.assertLess(emotion.pleasure, 0.0)

    def test_emotion_drives_safety_desire(self):
        profile, state, observation = context()
        desire = EmotionDrivenDesireModel().generate(
            PADState(pleasure=-0.8, arousal=0.9, dominance=-0.6, label="fear"),
            profile, state, observation,
        )
        self.assertIn(desire.primary, desire.scores)
        self.assertGreater(desire.scores["safety"], desire.scores["goal_progress"])

    def test_bounded_rational_choice_is_reproducible(self):
        profile, state, observation = context()
        emotion = PADContinuousEmotionModel(inertia=0).update(profile, state, observation)
        desire = EmotionDrivenDesireModel().generate(emotion, profile, state, observation)
        candidate = RouteCandidate("safe", "goal", 0, ("e", "goal"), 20)
        game = BoundedRationalGameModel()
        first = game.choose(profile, state, observation, emotion, desire, (candidate,))
        second = game.choose(profile, state, observation, emotion, desire, (candidate,))
        self.assertEqual(first, second)
        self.assertAlmostEqual(1.0, sum(first.probabilities.values()))

    def test_experience_replay_has_bounded_capacity_and_action_values(self):
        replay = ExperienceReplayBuffer(capacity=2)
        for index, reward in enumerate((1.0, -1.0, 0.5)):
            replay.append(Experience("p", f"s{index}", "visitor", {}, {}, "continue", None, reward))
        self.assertEqual(2, len(replay))
        self.assertAlmostEqual(-0.25, replay.action_values()["continue"])
        self.assertEqual(replay.sample(2, token="same"), replay.sample(2, token="same"))

    def test_pipeline_uses_selected_chain_and_records_experience(self):
        profile, state, observation = context()
        engine = IndividualModelingEngine(AgentDecisionEngine())
        engine.configure(implemented_architecture())
        plan = engine.rule_plan(profile, state, observation)
        self.assertEqual("bounded_rational", plan.source)
        self.assertEqual(1, len(engine.replay))
        self.assertTrue(engine.supports(profile))

    def test_pipeline_falls_back_for_unconfigured_role(self):
        profile, state, observation = context(role="other")
        engine = IndividualModelingEngine(AgentDecisionEngine())
        engine.configure(implemented_architecture())
        plan = engine.rule_plan(profile, state, observation)
        self.assertEqual("rule", plan.source)
        self.assertEqual(0, len(engine.replay))

    def test_runtime_configures_selected_chain_from_artificial_society(self):
        root = Path(__file__).resolve().parents[1]
        runtime = SimulationRuntime(
            root / "scenarios/shanghai_bund/bund.hotspot.sumocfg",
            sumo_binary=str(root / ".venv-backend/bin/sumo"),
        )
        runtime.requirement_id = "req-1"
        runtime.configure_artificial_society({
            "model_id": "asm-1",
            "schema_version": 2,
            "fingerprint": "f",
            "requirement_id": "req-1",
            "source": "manual",
            "capabilities": {},
            "artificial_society": {
                "individual_model": {
                    "profile_parameters": {},
                    "agent_types": implemented_architecture(),
                },
                "rule_model": {"runtime_parameters": {"decision_period_seconds": 3.0}},
            },
        })
        self.assertTrue(runtime.use_llm)
        self.assertTrue(runtime.decision_engine.role_configs["visitor"].enabled)
        self.assertEqual(3.0, runtime.decision_scheduler.period_seconds)
        diagnostics = runtime.artificial_society_diagnostics()["modeling_algorithms"]
        self.assertEqual("modeling_algorithm.individual_model.pipeline", diagnostics["backend"])

    def test_llm_decision_accepts_only_supplied_candidate(self):
        profile, state, observation = context()
        candidate = RouteCandidate("safe", "goal", 0, ("e", "goal"), 20)
        model = LLMDrivenDecisionModel()

        async def request(messages):
            return {"action": "reroute", "target_id": "safe", "reason": "avoid risk", "confidence": 0.9}

        model._request = request
        plan = asyncio.run(model.decide(
            profile, state, observation, (candidate,), PADState(),
            EmotionDrivenDesireModel().generate(PADState(), profile, state, observation),
            BoundedRationalGameModel().choose(
                profile, state, observation, PADState(),
                EmotionDrivenDesireModel().generate(PADState(), profile, state, observation),
                (candidate,),
            ),
        ))
        self.assertEqual("llm_driven", plan.source)
        self.assertEqual(candidate.edges, plan.route_edges)

    def test_llm_failure_uses_bounded_rational_fallback(self):
        profile, state, observation = context()
        model = LLMDrivenDecisionModel()

        async def request(messages):
            raise RuntimeError("offline")

        model._request = request
        desire = EmotionDrivenDesireModel().generate(PADState(), profile, state, observation)
        choice = BoundedRationalGameModel().choose(profile, state, observation, PADState(), desire)
        plan = asyncio.run(model.decide(profile, state, observation, (), PADState(), desire, choice))
        self.assertEqual("bounded_rational_fallback", plan.source)
        self.assertIn("offline", plan.reason)


if __name__ == "__main__":
    unittest.main()
