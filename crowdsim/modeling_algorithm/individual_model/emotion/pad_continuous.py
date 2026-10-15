"""PAD continuous emotion model for pedestrian agents."""

from __future__ import annotations

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, Observation
from crowdsim.modeling_algorithm.contracts import PADState


METHOD_NAME = "PAD 连续情感模型"
IMPLEMENTED = True


def _clip(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


class PADContinuousEmotionModel:
    """Update pleasure, arousal and dominance from one frozen simulation boundary.

    The equations intentionally use only observable runtime variables. Exponential
    smoothing gives emotional continuity without introducing wall-clock state.
    """

    method_name = METHOD_NAME

    def __init__(self, *, inertia: float = 0.65) -> None:
        self.inertia = _clip(inertia, 0.0, 0.95)

    def update(
        self,
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        previous: PADState | None = None,
    ) -> PADState:
        previous = previous or PADState()
        risk = _clip(max(state.perceived_risk, observation.perceived_risk), 0.0, 1.0)
        crowding = _clip(observation.perceived_crowding, 0.0, 1.0)
        stress = _clip(state.stress, 0.0, 1.0)
        fatigue = _clip(state.fatigue, 0.0, 1.0)
        blockage = _clip(state.blocked_duration / 30.0, 0.0, 1.0)
        speed_ratio = _clip(
            observation.own_motion.speed / max(0.1, profile.free_walking_speed * profile.mobility),
            0.0,
            1.0,
        )

        target_pleasure = _clip(
            0.35 * (1.0 - risk)
            + 0.20 * (1.0 - crowding)
            + 0.20 * speed_ratio
            + 0.10 * profile.familiarity
            - 0.20 * stress
            - 0.15 * fatigue
            - 0.15,
        )
        target_arousal = _clip(
            0.40 * risk
            + 0.30 * crowding
            + 0.20 * stress
            + 0.10 * blockage,
            0.0,
            1.0,
        )
        target_dominance = _clip(
            0.30 * profile.familiarity
            + 0.20 * profile.mobility
            + 0.20 * profile.authority_compliance
            + 0.15 * speed_ratio
            - 0.25 * risk
            - 0.20 * blockage
            - 0.20,
        )

        retain = self.inertia
        pleasure = _clip(retain * previous.pleasure + (1.0 - retain) * target_pleasure)
        arousal = _clip(retain * previous.arousal + (1.0 - retain) * target_arousal, 0.0, 1.0)
        dominance = _clip(retain * previous.dominance + (1.0 - retain) * target_dominance)
        return PADState(pleasure, arousal, dominance, self._label(pleasure, arousal, dominance))

    @staticmethod
    def _label(pleasure: float, arousal: float, dominance: float) -> str:
        if pleasure < -0.25 and arousal >= 0.55 and dominance < 0.0:
            return "fear"
        if pleasure < -0.20 and arousal >= 0.55 and dominance >= 0.0:
            return "anger"
        if pleasure >= 0.25 and arousal >= 0.45:
            return "excited"
        if pleasure >= 0.20 and arousal < 0.45:
            return "content"
        if arousal < 0.25:
            return "calm"
        return "alert"
