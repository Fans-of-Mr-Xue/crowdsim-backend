"""Evaluate applied decisions over a fixed simulation-time window."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .contracts import new_id, public_time


class EffectEvaluator:
    @staticmethod
    def evaluate(decision: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
        before_global = dict(before.get("global") or {})
        after_global = dict(after.get("global") or {})
        risk_before = float(before_global.get("riskIndex") or 0.0)
        risk_after = float(after_global.get("riskIndex") or 0.0)
        eff_before = float(before_global.get("efficiency") or 0.0)
        eff_after = float(after_global.get("efficiency") or 0.0)
        before_regions = {str(item.get("regionId") or item.get("id")): item for item in before.get("regions", [])}
        after_regions = {str(item.get("regionId") or item.get("id")): item for item in after.get("regions", [])}
        spillover = 0.0
        for region_id, item in after_regions.items():
            old = before_regions.get(region_id, {})
            spillover = max(spillover, float(item.get("riskIndex") or 0.0) - float(old.get("riskIndex") or 0.0))
        objective = max(-1.0, min(1.0, 0.65 * (risk_before - risk_after) + 0.35 * (eff_after - eff_before) - 0.35 * max(0.0, spillover)))
        return {
            "schemaVersion": "1.0",
            "evaluationId": new_id("eval"),
            "runId": decision["runId"],
            "decisionId": decision["decisionId"],
            "window": {"start": before["simTimeSeconds"], "end": after["simTimeSeconds"]},
            "before": deepcopy(before_global),
            "after": deepcopy(after_global),
            "delta": {"riskReduction": risk_before - risk_after, "efficiency": eff_after - eff_before},
            "spillover": {"riskIncrease": max(0.0, spillover)},
            "objectiveScore": objective,
            "afterObservation": deepcopy(dict(after)),
            "completedAt": public_time(),
        }
