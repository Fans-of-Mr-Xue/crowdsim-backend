"""DeepSeek-backed decision engine that returns CrowdSim ``BehaviorPlan`` objects."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Mapping, TYPE_CHECKING

from crowdsim.decision.route_provider import RouteCandidate
from crowdsim.domain.crowdsim_models import (
    AgentProfile,
    AgentState,
    BehaviorPlan,
    Observation,
)

from .contracts import (
    ALLOWED_ACTIONS,
    DENSITY_LEVELS,
    PedestrianDecisionContext,
    PedestrianDecisionResult,
)

if TYPE_CHECKING:
    from .client import DeepSeekClient


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _bounded_number(
    value: Any,
    *,
    default: float = 0.0,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    number = max(minimum, _number(value, default))
    return min(maximum, number) if maximum is not None else number


def normalize_context(context: dict) -> PedestrianDecisionContext:
    """Return a new, bounded context containing only supported model fields."""
    if not isinstance(context, dict):
        raise TypeError("pedestrian decision context must be a dictionary")

    profile = _mapping(context.get("profile"))
    state = _mapping(context.get("current_state"))
    crowd = _mapping(context.get("surrounding_crowd"))
    density_level = _text(crowd.get("density_level"), "unknown").lower()
    if density_level not in DENSITY_LEVELS:
        density_level = "unknown"

    normalized_candidates = []
    candidates = context.get("candidates")
    if isinstance(candidates, (list, tuple)):
        seen = set()
        for value in candidates:
            item = _mapping(value)
            target_id = _text(item.get("target_id"), "")
            if not target_id or target_id in seen:
                continue
            seen.add(target_id)
            normalized_candidates.append(
                {
                    "target_id": target_id,
                    "target_kind": _text(item.get("target_kind"), "route"),
                    "cost_seconds": _bounded_number(item.get("cost_seconds")),
                }
            )

    density_value = crowd.get("local_density")
    local_density = None if density_value is None else _bounded_number(density_value)
    return {
        "agent_id": _text(context.get("agent_id"), ""),
        "snapshot_id": _text(context.get("snapshot_id"), ""),
        "time_seconds": _bounded_number(context.get("time_seconds")),
        "profile": {
            "nationality": _text(profile.get("nationality"), "unspecified"),
            "language": _text(profile.get("language"), "unspecified"),
            "age_group": _text(profile.get("age_group"), "adult"),
            "risk_tolerance": _bounded_number(profile.get("risk_tolerance"), default=0.5, maximum=1.0),
            "crowding_tolerance": _bounded_number(profile.get("crowding_tolerance"), default=0.5, maximum=1.0),
            "familiarity": _bounded_number(profile.get("familiarity"), default=0.5, maximum=1.0),
            "patience": _bounded_number(profile.get("patience"), default=0.5, maximum=1.0),
        },
        "current_state": {
            "status": _text(state.get("status"), "walking"),
            "speed": _bounded_number(state.get("speed")),
            "stress": _bounded_number(state.get("stress"), maximum=1.0),
            "fatigue": _bounded_number(state.get("fatigue"), maximum=1.0),
            "flood_impact": _bounded_number(state.get("flood_impact"), maximum=1.0),
            "event_impact": _bounded_number(state.get("event_impact"), maximum=1.0),
            "perceived_risk": _bounded_number(state.get("perceived_risk"), maximum=1.0),
            "blocked_duration": _bounded_number(state.get("blocked_duration")),
        },
        "surrounding_crowd": {
            "nearby_people": int(_bounded_number(crowd.get("nearby_people"))),
            "local_density": local_density,
            "density_level": density_level,
            "perceived_crowding": _bounded_number(crowd.get("perceived_crowding"), maximum=1.0),
        },
        "candidates": normalized_candidates,
    }


def build_decision_context(
    profile: AgentProfile,
    state: AgentState,
    observation: Observation,
    candidates: Iterable[RouteCandidate] = (),
) -> PedestrianDecisionContext:
    """Build the model input from one immutable simulation boundary."""
    return normalize_context(
        {
            "agent_id": profile.person_id,
            "snapshot_id": observation.snapshot_id,
            "time_seconds": observation.time_seconds,
            "profile": {
                "nationality": profile.nationality,
                "language": profile.native_language,
                "age_group": profile.age_group,
                "risk_tolerance": profile.risk_tolerance,
                "crowding_tolerance": profile.crowding_tolerance,
                "familiarity": profile.familiarity,
                "patience": profile.patience,
            },
            "current_state": {
                "status": state.activity_state,
                "speed": observation.own_motion.speed,
                "stress": state.stress,
                "fatigue": state.fatigue,
                "flood_impact": observation.flood_impact,
                "event_impact": observation.event_impact,
                "perceived_risk": observation.perceived_risk,
                "blocked_duration": state.blocked_duration,
            },
            "surrounding_crowd": {
                "nearby_people": max(0, observation.local_people_count - 1),
                "local_density": observation.objective_density_per_m2,
                "density_level": observation.density_level,
                "perceived_crowding": observation.perceived_crowding,
            },
            "candidates": [
                {
                    "target_id": item.target_id,
                    "target_kind": item.target_kind,
                    "cost_seconds": item.estimated_cost_seconds,
                }
                for item in candidates
            ],
        }
    )


class PedestrianDecisionSkill:
    """Drop-in decision engine for ``DecisionScheduler``."""

    ALLOWED_ACTIONS = ALLOWED_ACTIONS

    def __init__(
        self,
        client: DeepSeekClient | None = None,
        *,
        config_path: str | Path | None = None,
    ) -> None:
        if client is not None and config_path is not None:
            raise ValueError("pass either client or config_path, not both")
        self._client = client
        self.last_error = ""
        if self._client is None:
            from .client import DeepSeekClient
            from .config import DeepSeekConfigError

            try:
                self._client = DeepSeekClient(config_path=config_path)
            except DeepSeekConfigError as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
        self.enabled = self._client is not None
        client_config = getattr(self._client, "config", {}) if self._client is not None else {}
        self.model = str(client_config.get("model", getattr(self._client, "model_id", "")))
        self.llm_decisions = 0
        self.fallback_decisions = 0

    @staticmethod
    def normalize_context(context: dict) -> PedestrianDecisionContext:
        return normalize_context(context)

    @staticmethod
    def build_context(
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        candidates: Iterable[RouteCandidate] = (),
    ) -> PedestrianDecisionContext:
        return build_decision_context(profile, state, observation, candidates)

    @staticmethod
    def build_messages(context: dict) -> list[dict[str, str]]:
        from .prompts import build_messages

        return build_messages(context)

    @staticmethod
    def parse_decision(
        raw_output: str,
        candidate_targets: Mapping[str, str] | None = None,
    ) -> PedestrianDecisionResult:
        from .validation import parse_decision

        return parse_decision(raw_output, candidate_targets)

    def rule_plan(
        self,
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        candidates: Iterable[RouteCandidate] = (),
    ) -> BehaviorPlan:
        from .fallback import fallback_decision

        candidates = tuple(candidates)
        decision = fallback_decision(build_decision_context(profile, state, observation, candidates))
        return self._to_plan(decision, profile, observation, candidates, source="rule")

    async def decide(
        self,
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        candidates: Iterable[RouteCandidate] = (),
    ) -> BehaviorPlan:
        """Call DeepSeek and return a complete plan, with a deterministic fallback."""
        from .client import DeepSeekClientError
        from .fallback import fallback_decision
        from .prompts import build_messages
        from .validation import DecisionValidationError, parse_decision

        candidates = tuple(candidates)
        context = build_decision_context(profile, state, observation, candidates)
        candidate_targets = {item.target_id: item.target_kind for item in candidates}
        if not self.enabled or self._client is None:
            self.fallback_decisions += 1
            decision = fallback_decision(context)
            return self._to_plan(decision, profile, observation, candidates, source="rule_fallback")
        try:
            raw_output = await self._client.complete(build_messages(context))
            decision = parse_decision(raw_output, candidate_targets)
            plan = self._to_plan(decision, profile, observation, candidates, source="llm")
            self.llm_decisions += 1
            return plan
        except (DeepSeekClientError, DecisionValidationError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.fallback_decisions += 1
            decision = fallback_decision(context)
            return self._to_plan(decision, profile, observation, candidates, source="rule_fallback")

    @staticmethod
    def _to_plan(
        decision: PedestrianDecisionResult,
        profile: AgentProfile,
        observation: Observation,
        candidates: tuple[RouteCandidate, ...],
        *,
        source: str,
    ) -> BehaviorPlan:
        action = decision["action"]
        common = {
            "person_id": profile.person_id,
            "snapshot_id": observation.snapshot_id,
            "proposed_action": action,
            "reason": decision["reason"],
            "confidence": decision["confidence"],
            "source": source,
            "decided_at": observation.time_seconds,
        }
        if action == "slow_down":
            return BehaviorPlan(
                **common,
                speed_limit=max(0.2, profile.free_walking_speed * profile.mobility * 0.65),
            )
        if action == "wait":
            return BehaviorPlan(**common, wait_until=observation.time_seconds + 2.0)
        if action in {"reroute", "change_goal"}:
            candidate = next(
                (item for item in candidates if item.target_id == decision["target_id"]),
                None,
            )
            if candidate is None:
                raise ValueError("decision target is not an available route candidate")
            return BehaviorPlan(
                **common,
                target_id=candidate.target_id,
                route_edges=candidate.edges,
                activity_duration=candidate.activity_duration if action == "change_goal" else None,
                next_route_edges=candidate.next_route_edges if action == "change_goal" else (),
            )
        return BehaviorPlan(**common)

    def diagnostics(self) -> dict:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "backend": "pedestrian_decision_skill",
            "llm_decisions": self.llm_decisions,
            "fallback_decisions": self.fallback_decisions,
            "last_error": self.last_error,
        }
