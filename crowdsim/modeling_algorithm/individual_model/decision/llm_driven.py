"""LLM-driven pedestrian decision with strict action validation and fallback."""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

from crowdsim.decision.route_provider import RouteCandidate, select_candidate
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, BehaviorPlan, Observation
from crowdsim.modeling_algorithm.contracts import DesireState, GameChoice, PADState


METHOD_NAME = "LLM-driven"
IMPLEMENTED = True
_ALLOWED_ACTIONS = {"continue", "slow_down", "wait", "reroute", "change_goal"}


class LLMDrivenDecisionModel:
    """Call an OpenAI-compatible model and emit only executable BehaviorPlan data."""

    method_name = METHOD_NAME

    _DEFAULT_BASE_URL = "http://127.0.0.1:8800/v1"

    def __init__(self, *, timeout_seconds: float | None = None) -> None:
        self.base_url = os.environ.get("CROWDSIM_LLM_BASE_URL", self._DEFAULT_BASE_URL).rstrip("/")
        self.model = os.environ.get("CROWDSIM_LLM_MODEL", "GLM-5.3-Flash")
        self.api_key = os.environ.get("CROWDSIM_MODEL_API_KEY", "")
        self.timeout_seconds = float(timeout_seconds or os.environ.get("CROWDSIM_LLM_DECISION_TIMEOUT", "3"))
        self.cooldown_seconds = float(os.environ.get("CROWDSIM_LLM_FAILURE_COOLDOWN", "30"))
        # Explicit local QA protocol proof: permits a key-less local endpoint for an
        # actual HTTP call. It is not a real external upstream and never enables a
        # public product path.
        self.qa_local = os.environ.get("CROWDSIM_LLM_QA_LOCAL", "").strip().lower() in {"1", "true", "yes", "on"}
        self.extra_body = self._extra_body()
        self.candidate_contract = False
        # Advisory learner scores keyed by candidate_id; never replaces the
        # supplied candidate identity contract. Warm-up missing scores stay None.
        self.advisory_scores: dict[str, dict[str, Any]] = {}
        self.last_error = ""
        self.calls = 0
        self.successes = 0
        self.fallbacks = 0
        self._unavailable_until = 0.0

    @staticmethod
    def _local_endpoint_allowed(base_url: str) -> bool:
        """A qa_local endpoint must be http(s) on an explicit loopback interface."""
        try:
            parsed = urlparse(base_url)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        return (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}

    @property
    def _qa_local_valid(self) -> bool:
        return self.qa_local and self._local_endpoint_allowed(self.base_url)

    @property
    def _connector_configured(self) -> bool:
        """External mode requires a key; keyless local QA requires a loopback URL.

        A non-default URL alone (empty key) is not a configured external connector.
        The key value is never read or logged.
        """
        return bool(self.api_key) or self._qa_local_valid

    @property
    def online_ready(self) -> bool:
        """Connector is configured; does not prove reachability or a closed breaker."""
        return self._connector_configured

    @property
    def callable(self) -> bool:
        """Eligibility gate: configured connector and circuit breaker closed."""
        return self._connector_configured and time.monotonic() >= self._unavailable_until

    @property
    def online_mode(self) -> str:
        if bool(self.api_key):
            return "external"
        if self._qa_local_valid:
            return "local_qa"
        return "unconfigured"

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
        *, is_current=None, candidate_scores: dict[str, dict[str, Any]] | None = None,
    ) -> BehaviorPlan:
        candidates = tuple(candidates)
        self.advisory_scores = self._validate_advisory_scores(candidate_scores, candidates)
        if not self._connector_configured:
            self.fallbacks += 1
            return self._fallback_plan(profile, observation, candidates, game_choice, "LLM connector not configured")
        if time.monotonic() < self._unavailable_until:
            self.fallbacks += 1
            return self._fallback_plan(profile, observation, candidates, game_choice, "LLM circuit breaker open")
        try:
            self.calls += 1
            messages = self._messages(profile, state, observation, candidates, emotion, desire, game_choice,
                candidate_contract=self.candidate_contract, advisory_scores=self.advisory_scores)
            result = await self._request(messages)
            if is_current is not None and not is_current():
                return BehaviorPlan(profile.person_id, observation.snapshot_id, 'continue', source='discarded_decision', reason='decision boundary superseded', decided_at=observation.time_seconds)
            plan = self._parse_plan(result, profile, observation, candidates, candidate_contract=self.candidate_contract)
            self.successes += 1
            self.last_error = ""
            return plan
        except Exception as exc:
            if is_current is not None and not is_current():
                return BehaviorPlan(profile.person_id, observation.snapshot_id, 'continue', source='discarded_decision', reason='decision boundary superseded', decided_at=observation.time_seconds)
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
        # trust_env=False: local QA/explicit connectors must not inherit a system proxy.
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        if not isinstance(result, dict):
            raise TypeError("LLM decision response must be a JSON object")
        return result

    @staticmethod
    def _validate_advisory_scores(raw: dict[str, dict[str, Any]] | None,
        candidates: tuple[RouteCandidate, ...] = ()) -> dict[str, dict[str, Any]]:
        """Clean advisory learner scores. Score is finite or None (pre-warm-up).

        Unknown/stale candidate identities are dropped against the current boundary
        candidate set. Malformed or non-finite entries are also dropped; the advisory
        section never overrides the supplied candidate identity contract enforced by
        ``_parse_plan``.
        """
        if not raw:
            return {}
        boundary_ids = {c.candidate_id for c in candidates if c.candidate_id is not None}
        cleaned: dict[str, dict[str, Any]] = {}
        for candidate_id, entry in raw.items():
            if not isinstance(candidate_id, str) or not isinstance(entry, dict):
                continue
            if boundary_ids and candidate_id not in boundary_ids:
                continue
            action = entry.get("action")
            if not isinstance(action, list) or not all(isinstance(a, str) for a in action):
                continue
            score = entry.get("score")
            if score is not None:
                if type(score) not in (int, float) or not math.isfinite(score):
                    continue
                score = float(score)
            cleaned[candidate_id] = {"action": list(action), "score": score}
        return cleaned

    @staticmethod
    def _messages(
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        candidates: tuple[RouteCandidate, ...],
        emotion: PADState,
        desire: DesireState,
        game_choice: GameChoice,
        *, candidate_contract: bool = False,
        advisory_scores: dict[str, dict[str, Any]] | None = None,
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
            "advisory_learning_scores": [
                {"candidate_id": cid, "action": entry["action"], "score": entry["score"]}
                for cid, entry in (advisory_scores or {}).items()
            ],
            "allowed_actions": sorted(_ALLOWED_ACTIONS),
        }
        system = (
            "你是人群聚集仿真中的个体行为决策模块。只能根据输入的冻结快照选择可执行动作。"
            "输出 JSON，且只能包含 action、target_id、reason、confidence。"
            "reroute/change_goal 必须选择 candidates 中的 target_id；其余动作 target_id 必须为 null。"
            "advisory_learning_scores 仅为学习参考，不能据此脱离候选身份合同选择。"
            "不得生成道路、坐标、速度或仿真参数。"
        )
        if candidate_contract:
            context['bounded_rational_reference']['candidate_id'] = game_choice.candidate_id
            context['candidates'] = [{'candidate_id': c.candidate_id, 'target_id': c.target_id,
                                      'target_kind': c.target_kind, 'entry_edge': c.entry_edge,
                                      'cost_seconds': c.estimated_cost_seconds,
                                      'congestion_delay_seconds': c.congestion_delay_seconds,
                                      'entry_density_person_per_m2': c.entry_density_person_per_m2,
                                      'preserve_future_stages': c.target_kind == 'hotspot_route'} for c in candidates]
            system = ('你是个体行为决策模块，只依据本次冻结候选选择动作。输出JSON且只含action、candidate_id、reason、confidence。'
                      'reroute/change_goal必须复制候选的candidate_id，其余动作candidate_id必须为null。'
                      'change_goal只能选activity；热点只允许reroute并保留目的地与未来stage。不得生成道路、坐标、速度或新参数。')
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
        *, candidate_contract: bool = False,
    ) -> BehaviorPlan:
        action = str(result.get("action") or "")
        if action not in _ALLOWED_ACTIONS:
            raise ValueError(f"unsupported LLM action: {action}")
        if candidate_contract and (set(result) - {'action', 'candidate_id', 'reason', 'confidence'} or 'candidate_id' not in result):
            raise ValueError('candidate decision has unsupported or missing fields')
        target_id = result.get("target_id")
        candidate_id = result.get('candidate_id')
        candidate = None
        if action in {"reroute", "change_goal"}:
            candidate = select_candidate(candidates, candidate_id=candidate_id, target_id=target_id) if candidate_contract else next((item for item in candidates if item.target_id == target_id), None)
            if candidate is None:
                raise ValueError("LLM selected a target outside supplied candidates")
            if action == "change_goal" and candidate.target_kind != "activity":
                raise ValueError("change_goal requires an activity candidate")
        elif target_id is not None or candidate_id is not None:
            raise ValueError("non-route action must not include target or candidate identity")
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
        candidate = select_candidate(candidates, candidate_id=choice.candidate_id, target_id=choice.target_id) if choice.action in {'reroute', 'change_goal'} else None
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
            selected_entry_edge=candidate.entry_edge if candidate is not None and candidate.target_kind == "hotspot_route" else None,
            candidate_id=candidate.candidate_id if candidate is not None else None,
            preserve_future_stages=bool(candidate is not None and candidate.target_kind == "hotspot_route"),
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "method": self.method_name,
            "model": self.model,
            "base_url": self.base_url,
            "connector": "httpx_openai_compatible",
            "online_ready": self.online_ready,
            "callable": self.callable,
            "online_mode": self.online_mode,
            "calls": self.calls,
            "successes": self.successes,
            "fallbacks": self.fallbacks,
            "advisory_scores": len(self.advisory_scores),
            "last_error": self.last_error,
        }
