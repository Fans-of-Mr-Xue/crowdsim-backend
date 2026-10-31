"""Bounded-rational game model for pedestrian action selection."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import random
from typing import Iterable

from crowdsim.decision.route_provider import RouteCandidate
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, Observation
from crowdsim.modeling_algorithm.contracts import DesireState, GameChoice, PADState


METHOD_NAME = "有限理性博弈"
IMPLEMENTED = True


@dataclass(frozen=True)
class _Option:
    action: str
    target_id: str | None
    utility: float
    reason: str


class BoundedRationalGameModel:
    """Quantal-response selection over executable pedestrian actions.

    Utilities combine private desire, emotional state and observable congestion.
    The seeded draw models bounded rationality while remaining reproducible.
    """

    method_name = METHOD_NAME

    def __init__(self, *, rationality: float = 4.0) -> None:
        self.rationality = max(0.1, float(rationality))

    def choose(
        self,
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        emotion: PADState,
        desire: DesireState,
        candidates: Iterable[RouteCandidate] = (),
        action_bias: dict[str, float] | None = None,
    ) -> GameChoice:
        candidates = tuple(candidates)
        bias = action_bias or {}
        risk = max(state.perceived_risk, observation.perceived_risk)
        crowding = observation.perceived_crowding
        options = [
            _Option("continue", None, desire.scores["goal_progress"] - 0.65 * risk - 0.35 * crowding + bias.get("continue", 0.0), "continue toward current goal"),
            _Option("slow_down", None, 0.55 * desire.scores["avoid_crowding"] + 0.25 * desire.scores["safety"] + bias.get("slow_down", 0.0), "reduce exposure in dense space"),
            _Option("wait", None, 0.60 * desire.scores["rest"] + 0.20 * desire.scores["safety"] + bias.get("wait", 0.0), "wait while fatigue or blockage is high"),
        ]
        for candidate in candidates:
            normalized_cost = min(1.0, candidate.estimated_cost_seconds / 300.0)
            utility = (
                0.55 * desire.scores["safety"]
                + 0.25 * desire.scores["avoid_crowding"]
                + 0.15 * emotion.dominance
                - 0.35 * normalized_cost
                + bias.get("reroute", 0.0)
            )
            action = "change_goal" if candidate.target_kind == "activity" else "reroute"
            options.append(_Option(action, candidate.target_id, utility, f"bounded-rational choice of {candidate.target_id}"))

        values = [self.rationality * option.utility for option in options]
        peak = max(values)
        weights = [math.exp(value - peak) for value in values]
        total = sum(weights)
        probabilities = [weight / total for weight in weights]
        token = f"{profile.person_id}:{observation.snapshot_id}:{observation.time_seconds:.6f}"
        seed = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
        selected = random.Random(seed).choices(options, weights=probabilities, k=1)[0]
        labels = [f"{option.action}:{option.target_id or '-'}" for option in options]
        return GameChoice(
            action=selected.action,
            target_id=selected.target_id,
            utility=selected.utility,
            probabilities=dict(zip(labels, probabilities)),
            reason=selected.reason,
        )
