"""Deterministic fallback for the standalone DeepSeek skill."""

from .contracts import PedestrianDecisionResult
from .skill import normalize_context


DANGER_IMPACT_THRESHOLD = 0.7
HIGH_STRESS_THRESHOLD = 0.7
HIGH_FATIGUE_THRESHOLD = 0.8


def fallback_decision(context: dict) -> PedestrianDecisionResult:
    normalized = normalize_context(context)
    state = normalized["current_state"]
    crowd = normalized["surrounding_crowd"]
    candidates = normalized["candidates"]
    high_risk = max(
        state["flood_impact"],
        state["event_impact"],
        state["perceived_risk"],
    ) >= DANGER_IMPACT_THRESHOLD or crowd["density_level"] == "critical"

    if high_risk and candidates:
        candidate = min(candidates, key=lambda item: item["cost_seconds"])
        action = "change_goal" if candidate["target_kind"] == "activity" else "reroute"
        return {
            "action": action,
            "target_id": candidate["target_id"],
            "reason": "当前风险较高，采用本地规则选择可达候选目标",
            "confidence": 0.6,
            "source": "local_fallback",
        }
    if high_risk or crowd["density_level"] == "crowded":
        return {
            "action": "slow_down",
            "target_id": None,
            "reason": "当前风险或拥挤程度较高，采用本地规则减速",
            "confidence": 0.6,
            "source": "local_fallback",
        }
    if state["blocked_duration"] >= 5.0:
        return {
            "action": "wait",
            "target_id": None,
            "reason": "当前持续受阻，采用本地规则短暂等待",
            "confidence": 0.5,
            "source": "local_fallback",
        }
    if state["stress"] >= HIGH_STRESS_THRESHOLD or state["fatigue"] >= HIGH_FATIGUE_THRESHOLD:
        return {
            "action": "slow_down",
            "target_id": None,
            "reason": "当前压力或疲劳较高，采用本地规则减速",
            "confidence": 0.5,
            "source": "local_fallback",
        }
    return {
        "action": "continue",
        "target_id": None,
        "reason": "未发现明显风险，采用本地规则继续移动",
        "confidence": 0.4,
        "source": "local_fallback",
    }
