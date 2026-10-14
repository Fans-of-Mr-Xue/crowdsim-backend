"""Emotion-driven desire generation."""

from __future__ import annotations

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, Observation
from crowdsim.modeling_algorithm.contracts import DesireState, PADState


METHOD_NAME = "情感驱动欲望"
IMPLEMENTED = True


def _clip(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class EmotionDrivenDesireModel:
    """Translate PAD affect and pedestrian context into normalized desires."""

    method_name = METHOD_NAME

    def generate(
        self,
        emotion: PADState,
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
    ) -> DesireState:
        risk = _clip(max(state.perceived_risk, observation.perceived_risk))
        crowding = _clip(observation.perceived_crowding)
        blocked = _clip(state.blocked_duration / 30.0)
        negative_pleasure = _clip((1.0 - emotion.pleasure) / 2.0)
        low_dominance = _clip((1.0 - emotion.dominance) / 2.0)

        scores = {
            "safety": _clip(0.40 * risk + 0.25 * emotion.arousal + 0.20 * negative_pleasure + 0.15 * low_dominance),
            "avoid_crowding": _clip(0.55 * crowding + 0.20 * emotion.arousal + 0.15 * blocked + 0.10 * (1.0 - profile.crowding_tolerance)),
            "goal_progress": _clip(0.35 * (1.0 - risk) + 0.25 * emotion.dominance + 0.20 * profile.familiarity + 0.20 * profile.endurance),
            "group_cohesion": _clip(0.50 * profile.group_cohesion + 0.25 * profile.following_tendency + 0.25 * (1.0 - risk)),
            "follow_guidance": _clip(0.55 * profile.authority_compliance + 0.25 * profile.information_trust.get("official", 0.5) + 0.20 * risk),
            "rest": _clip(0.45 * state.fatigue + 0.30 * blocked + 0.25 * (1.0 - profile.patience)),
        }
        primary = max(scores, key=lambda name: (scores[name], name))
        return DesireState(scores=scores, primary=primary)
