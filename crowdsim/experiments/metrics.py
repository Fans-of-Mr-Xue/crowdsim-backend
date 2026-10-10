"""Auditable run metrics and experiment aggregation.

All integrals use simulation time and a left-rectangle rule. Missing source
fields stay ``None`` where a value cannot be supported by the frame stream.
"""

from __future__ import annotations

import math
from statistics import mean, median, pstdev
from typing import Any, Iterable


def _number(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    result = float(value)
    return result if math.isfinite(result) else default


def _gini(values: list[float]) -> float | None:
    cleaned = [max(0.0, value) for value in values if math.isfinite(value)]
    if not cleaned or sum(cleaned) <= 0:
        return None
    count = len(cleaned)
    return sum(abs(left - right) for left in cleaned for right in cleaned) / (2 * count * sum(cleaned))


def _coefficient_of_variation(values: list[float]) -> float | None:
    if len(values) < 2 or mean(values) == 0:
        return None
    return pstdev(values) / abs(mean(values))


def summarize_observations(
    observations: Iterable[dict[str, Any]],
    *,
    high_risk_threshold: float = 0.7,
    safe_density: float = 2.0,
) -> dict[str, Any]:
    payloads = sorted(
        [item.get("payload", item) for item in observations],
        key=lambda item: float(item.get("simTimeSeconds") or 0.0),
    )
    if not payloads:
        return {}
    final = payloads[-1]
    globals_ = [dict(item.get("global") or {}) for item in payloads]
    times = [float(item.get("simTimeSeconds") or 0.0) for item in payloads]
    risks = [float(_number(item.get("riskIndex"), 0.0) or 0.0) for item in globals_]
    densities = [_number(item.get("maxDensity")) for item in globals_]
    completion = [float(_number(item.get("completionRate"), 0.0) or 0.0) for item in globals_]
    active = [float(_number(item.get("activePopulation"), 0.0) or 0.0) for item in globals_]
    evacuated = [float(_number(item.get("evacuatedPopulation"), 0.0) or 0.0) for item in globals_]

    cumulative_risk = high_risk_person_seconds = excess_density_seconds = hotspot_seconds = 0.0
    for index in range(1, len(payloads)):
        delta_time = max(0.0, times[index] - times[index - 1])
        cumulative_risk += active[index - 1] * risks[index - 1] * delta_time
        if risks[index - 1] >= high_risk_threshold:
            high_risk_person_seconds += active[index - 1] * delta_time
            hotspot_seconds += delta_time
        density = densities[index - 1]
        if density is not None:
            excess_density_seconds += max(0.0, density - safe_density) * delta_time

    t95 = next((time_value for time_value, value in zip(times, completion) if value >= 0.95), None)
    duration = max(0.0, times[-1] - times[0])
    evacuated_delta = max(0.0, evacuated[-1] - evacuated[0])
    active_person_seconds = sum(
        active[index - 1] * max(0.0, times[index] - times[index - 1])
        for index in range(1, len(payloads))
    )
    rebound_transitions = sum(risks[index] > risks[index - 1] + 1e-9 for index in range(1, len(risks)))

    region_series: dict[str, list[float]] = {}
    max_region_risk = max_region_density = None
    exit_loads: list[float] = []
    for payload in payloads:
        for region in payload.get("regions") or []:
            region_id = str(region.get("regionId") or region.get("id") or "unknown")
            risk = _number(region.get("riskIndex"))
            density = _number(region.get("density"))
            if risk is not None:
                region_series.setdefault(region_id, []).append(risk)
                max_region_risk = risk if max_region_risk is None else max(max_region_risk, risk)
            if density is not None:
                max_region_density = density if max_region_density is None else max(max_region_density, density)
        loads = (payload.get("global") or {}).get("exitLoads")
        if isinstance(loads, dict):
            exit_loads.extend(float(value) for value in loads.values() if _number(value) is not None)

    region_means = [mean(values) for values in region_series.values() if values]
    return {
        "sampleCount": len(payloads),
        "durationSeconds": float(final.get("simTimeSeconds") or 0.0),
        "observedWindowSeconds": duration,
        "finalCompletionRate": completion[-1],
        "finalRiskIndex": risks[-1],
        "meanRiskIndex": mean(risks),
        "peakRiskIndex": max(risks),
        "peakDensity": max((value for value in densities if value is not None), default=None),
        "cumulativeRiskExposurePersonSeconds": cumulative_risk,
        "highRiskPersonMinutes": high_risk_person_seconds / 60.0,
        "cumulativeExcessDensitySeconds": excess_density_seconds,
        "t95Seconds": t95,
        "estimatedMeanEvacuationSeconds": active_person_seconds / evacuated_delta if evacuated_delta > 0 else None,
        "safeOutflowPerSimMinute": evacuated_delta / duration * 60.0 if duration > 0 else None,
        "unevacuatedFinal": active[-1],
        "maxRegionRisk": max_region_risk,
        "maxRegionDensity": max_region_density,
        "highRiskHotspotDurationSeconds": hotspot_seconds,
        "regionRiskGini": _gini(region_means),
        "exitLoadCoefficientVariation": _coefficient_of_variation(exit_loads),
        "riskReboundRate": rebound_transitions / max(1, len(risks) - 1),
    }


def summarize_control_records(
    decisions: Iterable[dict[str, Any]],
    acknowledgements: Iterable[dict[str, Any]],
    evaluations: Iterable[dict[str, Any]],
    llm_calls: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    decisions_ = [item.get("payload", item) for item in decisions]
    acks = [item.get("payload", item) for item in acknowledgements]
    evaluations_ = [item.get("payload", item) for item in evaluations]
    calls = [item.get("payload", item) for item in llm_calls]
    actions = [action for decision in decisions_ for action in (decision.get("actions") or [])]
    statuses = [str(item.get("status") or "") for item in acks]
    terminal_statuses = [status for status in statuses if status not in {"", "queued"}]
    applied = sum(status == "applied" for status in terminal_statuses)
    rejected = sum(status in {"rejected", "error", "failed"} for status in terminal_statuses)
    decision_latencies = [value for value in (_number(item.get("durationMs", item.get("latencyMs"))) for item in calls) if value is not None]
    def usage_value(call, camel, snake):
        usage = call.get("usage") or {}
        return _number(call.get(camel), _number(usage.get(camel), _number(usage.get(snake), 0.0))) or 0.0
    tokens = sum(float(usage_value(call, "inputTokens", "input_tokens")) + float(usage_value(call, "outputTokens", "output_tokens")) for call in calls)
    cost = sum(float(_number(call.get("cost"), 0.0) or 0.0) for call in calls)
    objectives = [value for value in (_number(item.get("objectiveScore")) for item in evaluations_) if value is not None]
    return {
        "decisionCount": len(decisions_),
        "actionCount": len(actions),
        "ackCount": len(acks),
        "appliedCount": applied,
        "rejectedCount": rejected,
        "appliedSuccessRate": applied / len(terminal_statuses) if terminal_statuses else None,
        "evaluationCount": len(evaluations_),
        "meanObjectiveScore": mean(objectives) if objectives else None,
        "meanDecisionLatencyMs": mean(decision_latencies) if decision_latencies else None,
        "llmCallCount": len(calls),
        "llmTokenCount": tokens,
        "llmEstimatedCost": cost,
    }


def aggregate_method_runs(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"count": 0}
    numeric_keys = sorted({key for item in items for key, value in item.items() if isinstance(value, (int, float)) and not isinstance(value, bool)})
    result: dict[str, Any] = {"count": len(items)}
    for key in numeric_keys:
        values = [float(item[key]) for item in items if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool) and math.isfinite(float(item[key]))]
        if not values:
            continue
        result[key] = {"mean": mean(values), "median": median(values), "std": pstdev(values) if len(values) > 1 else 0.0, "min": min(values), "max": max(values)}
    return result
