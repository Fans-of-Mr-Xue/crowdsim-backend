"""LLM-driven pedestrian decision with strict action validation and fallback."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterable

import httpx

from crowdsim.decision.route_provider import RouteCandidate
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, BehaviorPlan, Observation
from crowdsim.modeling_algorithm.contracts import DesireState, GameChoice, PADState


METHOD_NAME = "LLM-driven"
IMPLEMENTED = True
_ALLOWED_ACTIONS = {"continue", "slow_down", "wait", "reroute", "change_goal"}


class LLMDrivenDecisionModel:
    """Call an OpenAI-compatible model and emit only executable BehaviorPlan data."""

    method_name = METHOD_NAME

    def __init__(self, *, timeout_seconds: float | None = None) -> None:
        self.base_url = os.environ.get("CROWDSIM_LLM_BASE_URL", "http://127.0.0.1:8800/v1").rstrip("/")
        self.model = os.environ.get("CROWDSIM_LLM_MODEL", "qwen3.8-flash-next")
        self.api_key = os.environ.get("CROWDSIM_MODEL_API_KEY", "")
        self.timeout_seconds = float(timeout_seconds or os.environ.get("CROWDSIM_LLM_DECISION_TIMEOUT", "3"))
        self.cooldown_seconds = float(os.environ.get("CROWDSIM_LLM_FAILURE_COOLDOWN", "30"))
        self.extra_body = self._extra_body()
        self.last_error = ""
        self.calls = 0
        self.successes = 0
        self.fallbacks = 0
        self._unavailable_until = 0.0

    @staticmethod
    def _extra_body() -> dict[str, Any]:
        raw = os.environ.get("CROWDSIM_LLM_EXTRA_BODY", "{}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    async def decide(
        self,
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        candidates: Iterable[RouteCandidate],
        emotion: PADState,
        desire: DesireState,
        game_choice: GameChoice,
    ) -> BehaviorPlan:
        candidates = tuple(candidates)
        if time.monotonic() < self._unavailable_until:
            self.fallbacks += 1
            return self._fallback_plan(profile, observation, candidates, game_choice, "LLM circuit breaker open")
        try:
            self.calls += 1
            result = await self._request(self._messages(profile, state, observation, candidates, emotion, desire, game_choice))
            plan = self._parse_plan(result, profile, observation, candidates)
            self.successes += 1
            self.last_error = ""
            return plan
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._unavailable_until = time.monotonic() + max(0.0, self.cooldown_seconds)
            self.fallbacks += 1
            return self._fallback_plan(profile, observation, candidates, game_choice, self.last_error)

    async def _request(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
            **self.extra_body,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        if not isinstance(result, dict):
            raise TypeError("LLM decision response must be a JSON object")
        return result

    @staticmethod
    def _messages(
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        candidates: tuple[RouteCandidate, ...],
        emotion: PADState,
        desire: DesireState,
        game_choice: GameChoice,
    ) -> list[dict[str, str]]:
        context = {
            "person_id": profile.person_id,
            "crowd_role": profile.crowd_role,
            "profile": {
                "risk_tolerance": profile.risk_tolerance,
                "crowding_tolerance": profile.crowding_tolerance,
                "following_tendency": profile.following_tendency,
                "authority_compliance": profile.authority_compliance,
            },
            "state": {
                "stress": state.stress,
                "fatigue": state.fatigue,
                "perceived_risk": state.perceived_risk,
                "blocked_duration": state.blocked_duration,
                "current_goal": state.current_goal,
            },
            "observation": {
                "snapshot_id": observation.snapshot_id,
                "time_seconds": observation.time_seconds,
                "speed": observation.own_motion.speed,
                "perceived_crowding": observation.perceived_crowding,
                "objective_density_per_m2": observation.objective_density_per_m2,
            },
            "pad_emotion": emotion.as_dict(),
            "desire": desire.as_dict(),
            "bounded_rational_reference": {
                "action": game_choice.action,
                "target_id": game_choice.target_id,
                "utility": game_choice.utility,
            },
            "candidates": [
                {"target_id": item.target_id, "target_kind": item.target_kind, "cost_seconds": item.estimated_cost_seconds}
                for item in candidates
            ],
            "allowed_actions": sorted(_ALLOWED_ACTIONS),
        }
        system = (
            "你是人群聚集仿真中的个体行为决策模块。只能根据输入的冻结快照选择可执行动作。"
            "输出 JSON，且只能包含 action、target_id、reason、confidence。"
            "reroute/change_goal 必须选择 candidates 中的 target_id；其余动作 target_id 必须为 null。"
            "不得生成道路、坐标、速度或仿真参数。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, sort_keys=True)},
        ]

    @staticmethod
    def _parse_plan(
        result: dict[str, Any],
        profile: AgentProfile,
        observation: Observation,
        candidates: tuple[RouteCandidate, ...],
    ) -> BehaviorPlan:
        action = str(result.get("action") or "")
        if action not in _ALLOWED_ACTIONS:
            raise ValueError(f"unsupported LLM action: {action}")
        target_id = result.get("target_id")
        candidate = None
        if action in {"reroute", "change_goal"}:
            candidate = next((item for item in candidates if item.target_id == target_id), None)
            if candidate is None:
                raise ValueError("LLM selected a target outside supplied candidates")
            if action == "change_goal" and candidate.target_kind != "activity":
                raise ValueError("change_goal requires an activity candidate")
        elif target_id is not None:
            raise ValueError("non-route action must not include target_id")
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.5))))
        return LLMDrivenDecisionModel._make_plan(
            profile, observation, action, candidate,
            reason=str(result.get("reason") or "LLM-driven decision")[:240],
            confidence=confidence,
            source="llm_driven",
        )

    @staticmethod
    def _fallback_plan(
        profile: AgentProfile,
        observation: Observation,
        candidates: tuple[RouteCandidate, ...],
        choice: GameChoice,
        error: str,
    ) -> BehaviorPlan:
        candidate = next((item for item in candidates if item.target_id == choice.target_id), None)
        action = choice.action if choice.action not in {"reroute", "change_goal"} or candidate is not None else "continue"
        return LLMDrivenDecisionModel._make_plan(
            profile, observation, action, candidate,
            reason=f"LLM fallback to bounded rationality: {error}"[:240],
            confidence=None,
            source="bounded_rational_fallback",
        )

    @staticmethod
    def _make_plan(profile, observation, action, candidate, *, reason, confidence, source) -> BehaviorPlan:
        route_edges = candidate.edges if candidate is not None else ()
        return BehaviorPlan(
            person_id=profile.person_id,
            snapshot_id=observation.snapshot_id,
            proposed_action=action,
            target_id=candidate.target_id if candidate is not None else None,
            route_edges=route_edges,
            speed_limit=max(0.2, profile.free_walking_speed * profile.mobility * 0.65) if action == "slow_down" else None,
            wait_until=observation.time_seconds + 2.0 if action == "wait" else None,
            activity_duration=candidate.activity_duration if candidate is not None and action == "change_goal" else None,
            next_route_edges=candidate.next_route_edges if candidate is not None and action == "change_goal" else (),
            arrival_position=candidate.arrival_position if candidate is not None else None,
            next_arrival_position=candidate.next_arrival_position if candidate is not None and action == "change_goal" else None,
            next_target_id=candidate.next_target_id if candidate is not None and action == "change_goal" else None,
            reason=reason,
            confidence=confidence,
            source=source,
            decided_at=observation.time_seconds,
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "method": self.method_name,
            "model": self.model,
            "base_url": self.base_url,
            "calls": self.calls,
            "successes": self.successes,
            "fallbacks": self.fallbacks,
            "last_error": self.last_error,
        }
