import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from crowdsim.decision.route_provider import RouteCandidate
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, BehaviorPlan, MotionSnapshot, Observation
from pedestrian_decision_skill import (
    ALLOWED_ACTIONS,
    DeepSeekClient,
    DeepSeekRequestError,
    DecisionValidationError,
    PedestrianDecisionSkill,
    build_decision_context,
    fallback_decision,
    normalize_context,
    parse_decision,
)


def backend_values(*, risk=0.0, density=0.2, density_level="free"):
    profile = AgentProfile(
        "p1",
        nationality="CN",
        native_language="zh",
        risk_tolerance=0.4,
        crowding_tolerance=0.4,
    )
    state = AgentState("p1", stress=0.3, fatigue=0.2, perceived_risk=risk)
    motion = MotionSnapshot("p1", 1.0, 1.0, 2.0, 121.0, 31.0, 1.1, "e1", "e1_0", 1.0, 90.0, 0, 2)
    observation = Observation(
        person_id="p1",
        snapshot_id="run:2",
        time_seconds=1.0,
        own_motion=motion,
        neighbour_ids=("p2", "p3"),
        local_people_count=3,
        local_area_m2=10.0,
        objective_density_per_m2=density,
        perceived_crowding=0.4,
        perceived_risk=risk,
        density_level=density_level,
        flood_impact=0.1,
        event_impact=risk,
    )
    return profile, state, observation


def route_candidate(kind="exit"):
    return RouteCandidate(
        target_id="safe-target",
        target_edge="e2",
        arrival_position=-1,
        edges=("e1", "e2"),
        estimated_cost_seconds=12.0,
        target_kind=kind,
        activity_duration=30.0 if kind == "activity" else None,
        next_route_edges=("e2", "e3") if kind == "activity" else (),
    )


class StubDecisionClient:
    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.messages = None
        self.config = {"model": "stub-deepseek"}

    async def complete(self, messages):
        self.messages = messages
        if self.error:
            raise self.error
        return self.output


class ContextTests(unittest.TestCase):
    def test_action_vocabulary_matches_backend(self):
        self.assertEqual(
            {"continue", "slow_down", "wait", "reroute", "change_goal"},
            set(ALLOWED_ACTIONS),
        )

    def test_backend_context_contains_requested_state(self):
        profile, state, observation = backend_values(risk=0.6, density=1.8, density_level="crowded")
        context = build_decision_context(profile, state, observation, [route_candidate()])
        self.assertEqual("CN", context["profile"]["nationality"])
        self.assertEqual("zh", context["profile"]["language"])
        self.assertEqual(1.1, context["current_state"]["speed"])
        self.assertEqual(0.6, context["current_state"]["event_impact"])
        self.assertEqual(2, context["surrounding_crowd"]["nearby_people"])
        self.assertEqual(1.8, context["surrounding_crowd"]["local_density"])
        self.assertEqual("safe-target", context["candidates"][0]["target_id"])
        self.assertNotIn("edges", context["candidates"][0])

    def test_normalization_clamps_untrusted_values(self):
        normalized = normalize_context(
            {
                "profile": {"nationality": " CN ", "risk_tolerance": 7},
                "current_state": {"stress": -2, "fatigue": "2"},
                "surrounding_crowd": {"nearby_people": -3, "density_level": "BAD"},
                "candidates": [{"target_id": "x", "cost_seconds": -4}, {"target_id": "x"}],
            }
        )
        self.assertEqual("CN", normalized["profile"]["nationality"])
        self.assertEqual(1.0, normalized["profile"]["risk_tolerance"])
        self.assertEqual(0.0, normalized["current_state"]["stress"])
        self.assertEqual(1.0, normalized["current_state"]["fatigue"])
        self.assertEqual(0, normalized["surrounding_crowd"]["nearby_people"])
        self.assertEqual("unknown", normalized["surrounding_crowd"]["density_level"])
        self.assertEqual(1, len(normalized["candidates"]))

    def test_prompt_contains_context_but_not_route_edges(self):
        profile, state, observation = backend_values()
        context = build_decision_context(profile, state, observation, [route_candidate()])
        messages = PedestrianDecisionSkill.build_messages(context)
        self.assertIn("nationality", messages[1]["content"])
        self.assertIn("safe-target", messages[1]["content"])
        self.assertNotIn('"edges"', messages[1]["content"])
        for action in ALLOWED_ACTIONS:
            self.assertIn(action, messages[0]["content"])


class ValidationTests(unittest.TestCase):
    def test_continue_result_is_valid(self):
        result = parse_decision(
            '{"action":"continue","target_id":null,"reason":"环境安全","confidence":0.9}'
        )
        self.assertEqual("llm", result["source"])

    def test_reroute_must_use_supplied_candidate(self):
        raw = '{"action":"reroute","target_id":"safe-target","reason":"避开风险","confidence":0.8}'
        self.assertEqual("safe-target", parse_decision(raw, {"safe-target": "exit"})["target_id"])
        with self.assertRaisesRegex(DecisionValidationError, "available candidate"):
            parse_decision(raw, {})

    def test_change_goal_requires_activity_candidate(self):
        raw = '{"action":"change_goal","target_id":"safe-target","reason":"前往目标","confidence":0.7}'
        with self.assertRaisesRegex(DecisionValidationError, "activity"):
            parse_decision(raw, {"safe-target": "exit"})
        self.assertEqual("change_goal", parse_decision(raw, {"safe-target": "activity"})["action"])

    def test_non_route_action_rejects_target(self):
        raw = '{"action":"wait","target_id":"safe-target","reason":"等待","confidence":0.5}'
        with self.assertRaisesRegex(DecisionValidationError, "null"):
            parse_decision(raw, {"safe-target": "exit"})

    def test_extra_duplicate_and_invalid_fields_are_rejected(self):
        samples = [
            '{"action":"continue","target_id":null,"reason":"安全","confidence":1,"extra":1}',
            '{"action":"continue","action":"wait","target_id":null,"reason":"安全","confidence":1}',
            '{"action":"avoid","target_id":null,"reason":"安全","confidence":1}',
            '{"action":"continue","target_id":null,"reason":"安全","confidence":2}',
        ]
        for raw in samples:
            with self.subTest(raw=raw), self.assertRaises(DecisionValidationError):
                parse_decision(raw)


class FallbackTests(unittest.TestCase):
    def test_high_risk_uses_available_route(self):
        profile, state, observation = backend_values(risk=0.9, density_level="critical")
        result = fallback_decision(build_decision_context(profile, state, observation, [route_candidate()]))
        self.assertEqual("reroute", result["action"])
        self.assertEqual("safe-target", result["target_id"])

    def test_high_risk_without_route_slows_down(self):
        profile, state, observation = backend_values(risk=0.9, density_level="critical")
        result = fallback_decision(build_decision_context(profile, state, observation))
        self.assertEqual("slow_down", result["action"])


class SkillPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_continue_returns_behavior_plan(self):
        client = StubDecisionClient(
            output='{"action":"continue","target_id":null,"reason":"状态稳定","confidence":0.9}'
        )
        skill = PedestrianDecisionSkill(client=client)
        plan = await skill.decide(*backend_values())
        self.assertIsInstance(plan, BehaviorPlan)
        self.assertEqual("continue", plan.proposed_action)
        self.assertEqual("run:2", plan.snapshot_id)
        self.assertEqual(0.9, plan.confidence)
        self.assertEqual("llm", plan.source)

    async def test_llm_slow_down_uses_deterministic_speed(self):
        client = StubDecisionClient(
            output='{"action":"slow_down","target_id":null,"reason":"局部拥挤","confidence":0.8}'
        )
        skill = PedestrianDecisionSkill(client=client)
        profile, state, observation = backend_values()
        plan = await skill.decide(profile, state, observation)
        self.assertAlmostEqual(profile.free_walking_speed * profile.mobility * 0.65, plan.speed_limit)

    async def test_llm_reroute_copies_trusted_candidate_edges(self):
        client = StubDecisionClient(
            output='{"action":"reroute","target_id":"safe-target","reason":"避开风险","confidence":0.8}'
        )
        skill = PedestrianDecisionSkill(client=client)
        candidate = route_candidate()
        plan = await skill.decide(*backend_values(), [candidate])
        self.assertEqual(candidate.edges, plan.route_edges)
        self.assertEqual(candidate.target_id, plan.target_id)

    async def test_change_goal_copies_activity_stages(self):
        client = StubDecisionClient(
            output='{"action":"change_goal","target_id":"safe-target","reason":"前往活动点","confidence":0.75}'
        )
        skill = PedestrianDecisionSkill(client=client)
        candidate = route_candidate("activity")
        plan = await skill.decide(*backend_values(), [candidate])
        self.assertEqual(30.0, plan.activity_duration)
        self.assertEqual(candidate.next_route_edges, plan.next_route_edges)

    async def test_request_and_validation_failures_use_rule_plan(self):
        cases = [
            StubDecisionClient(error=DeepSeekRequestError("offline")),
            StubDecisionClient(output='{"action":"avoid"}'),
        ]
        for client in cases:
            with self.subTest(client=client):
                skill = PedestrianDecisionSkill(client=client)
                plan = await skill.decide(*backend_values(risk=0.9))
                self.assertEqual("rule_fallback", plan.source)
                self.assertEqual("slow_down", plan.proposed_action)

    async def test_unexpected_errors_are_not_hidden(self):
        skill = PedestrianDecisionSkill(client=StubDecisionClient(error=RuntimeError("bug")))
        with self.assertRaisesRegex(RuntimeError, "bug"):
            await skill.decide(*backend_values())

    async def test_invalid_config_disables_llm_but_keeps_rule_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            skill = PedestrianDecisionSkill(config_path=path)
            plan = await skill.decide(*backend_values())
        self.assertFalse(skill.enabled)
        self.assertEqual("rule_fallback", plan.source)


class DeepSeekClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_sends_expected_request(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers["Authorization"]
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"action":"continue","target_id":null,"reason":"安全","confidence":1}'}}]},
            )

        client = DeepSeekClient(
            {
                "api_key": "secret-key",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-test",
                "timeout_seconds": 5.0,
                "max_tokens": 160,
                "temperature": 0.1,
            },
            transport=httpx.MockTransport(handler),
        )
        content = await client.complete([{"role": "user", "content": "test"}])
        self.assertIn('"action":"continue"', content)
        self.assertEqual("Bearer secret-key", captured["authorization"])
        self.assertEqual("deepseek-test", captured["payload"]["model"])


if __name__ == "__main__":
    unittest.main()
