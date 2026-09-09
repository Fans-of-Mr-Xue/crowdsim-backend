"""Small deterministic fallback used when an LLM decision is unavailable."""

from .contracts import PedestrianDecisionContext, PedestrianDecisionResult
from .skill import normalize_context


DANGER_IMPACT_THRESHOLD = 0.7
HIGH_STRESS_THRESHOLD = 0.7
HIGH_FATIGUE_THRESHOLD = 0.8


def fallback_decision(context: dict) -> PedestrianDecisionResult:
    """Return one conservative action using only normalized v1 fields."""
    normalized: PedestrianDecisionContext = normalize_context(context)
    state = normalized["current_state"]
    crowd = normalized["surrounding_crowd"]

    if crowd["density_level"] == "critical":
        return {
            "action": "avoid",
            "reason": "人群密度达到危险等级，采用本地规则避让",
            "confidence": 0.6,
            "source": "local_fallback",
        }
    if state["event_impact"] >= DANGER_IMPACT_THRESHOLD:
        return {
            "action": "avoid",
            "reason": "事件影响较高，采用本地规则避让",
            "confidence": 0.6,
            "source": "local_fallback",
        }
    if state["flood_impact"] >= DANGER_IMPACT_THRESHOLD:
        return {
            "action": "avoid",
            "reason": "积水影响较高，采用本地规则避让",
            "confidence": 0.6,
            "source": "local_fallback",
        }

    if crowd["density_level"] == "crowded":
        return {
            "action": "slow_down",
            "reason": "周围人群拥挤，采用本地规则减速",
            "confidence": 0.5,
            "source": "local_fallback",
        }
    if state["stress"] >= HIGH_STRESS_THRESHOLD:
        return {
            "action": "slow_down",
            "reason": "当前压力较高，采用本地规则减速",
            "confidence": 0.5,
            "source": "local_fallback",
        }
    if state["fatigue"] >= HIGH_FATIGUE_THRESHOLD:
        return {
            "action": "slow_down",
            "reason": "当前疲劳程度较高，采用本地规则减速",
            "confidence": 0.5,
            "source": "local_fallback",
        }

    return {
        "action": "continue",
        "reason": "未发现明显风险，采用本地规则继续移动",
        "confidence": 0.4,
        "source": "local_fallback",
    }
