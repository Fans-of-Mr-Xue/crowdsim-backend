from __future__ import annotations

from typing import Any, Mapping

from .base import BaseController


class RuleBasedController(BaseController):
    controller_id = "rule_based"

    def should_decide(self, observation: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> bool:
        return True

    def decide(self, observation: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        global_metrics = dict(observation.get("global") or {})
        risk = float(global_metrics.get("riskIndex") or 0.0)
        high = float(self.config.get("highRiskThreshold", 0.7))
        medium = float(self.config.get("mediumRiskThreshold", 0.4))
        validity = float(self.config.get("actionValiditySeconds", 120))
        regions = list(observation.get("regions") or []) or [{"regionId": "global", **global_metrics}]
        actions = []
        fired = []
        for region in regions:
            region_id = str(region.get("regionId") or region.get("id") or "global")
            region_risk = float(region.get("riskIndex", risk) or risk)
            if region_risk >= high and region.get("entryId"):
                actions.append({
                    "actionType": "set_inflow_rate",
                    "target": {"entryId": str(region["entryId"])},
                    "parameters": {"rate": 0.45, "durationSeconds": validity},
                    "priority": 90,
                })
                fired.append(f"{region_id}:high")
            elif region_risk >= high and region.get("edgeId"):
                actions.append({
                    "actionType": "set_edge_risk_weight",
                    "target": {"edgeId": str(region["edgeId"])},
                    "parameters": {"weight": 2.5, "durationSeconds": validity},
                    "priority": 85,
                })
                fired.append(f"{region_id}:high")
            elif region_risk >= medium:
                actions.append({
                    "actionType": "publish_guidance",
                    "target": {"regionId": region_id},
                    "parameters": {"message": f"请按现场引导有序离开 {region_id}", "durationSeconds": validity},
                    "priority": 60,
                })
                fired.append(f"{region_id}:medium")
        if not actions:
            actions = [{"actionType": "observe_only", "target": {"regionId": "global"}, "parameters": {"reason": "below thresholds"}, "priority": 10}]
        return self._decision(
            observation,
            actions,
            f"C1 fixed thresholds evaluated risk={risk:.3f}",
            trigger=str((context or {}).get("trigger") or "observation_cycle"),
            algorithm_trace={"thresholds": {"medium": medium, "high": high}, "firedRules": fired},
        )
