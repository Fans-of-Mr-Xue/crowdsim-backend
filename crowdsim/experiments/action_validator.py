"""Capability-aware action validation before dispatch to CrowdSim."""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Mapping

from .contracts import ContractError, validate_action


class ActionValidator:
    def __init__(self, capabilities: Mapping[str, Any] | None = None) -> None:
        self.capabilities = dict(capabilities or {})

    def update_capabilities(self, capabilities: Mapping[str, Any] | None) -> None:
        self.capabilities = deepcopy(dict(capabilities or {}))

    def validate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        action = validate_action(payload)
        self._validate_shape(action)
        supported = (self.capabilities.get("supportedActions") or {})
        if supported and action["actionType"] not in supported:
            raise ContractError(f"CrowdSim does not support {action['actionType']}")
        params = action["parameters"]
        spec = supported.get(action["actionType"], {}) if isinstance(supported, Mapping) else {}
        for key, bounds in spec.items() if isinstance(spec, Mapping) else ():
            if key in {"implementation", "requires"} or not isinstance(bounds, Mapping) or key not in params:
                continue
            value = params[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if bounds.get("min") is not None and value < bounds["min"]:
                    raise ContractError(f"{key} is below CrowdSim capability minimum")
                if bounds.get("max") is not None and value > bounds["max"]:
                    raise ContractError(f"{key} exceeds CrowdSim capability maximum")
        return action

    @staticmethod
    def _number(value, name, low, high):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not low <= float(value) <= high:
            raise ContractError(f"{name} must be in [{low}, {high}]")
        return float(value)

    def _validate_shape(self, action):
        action_type, target, params = action["actionType"], action["target"], action["parameters"]
        if action_type == "publish_guidance":
            if not str(params.get("message") or "").strip():
                raise ContractError("publish_guidance requires message")
            command = str(params.get("command") or "inform")
            if command not in {"inform", "reroute", "disperse", "evacuate", "queue"}:
                raise ContractError("publish_guidance command is unsupported")
            params["command"] = command
        if action_type == "set_inflow_rate":
            if not str(target.get("entryId") or ""):
                raise ContractError("set_inflow_rate requires entryId")
            params["rate"] = self._number(params.get("rate"), "rate", 0, 1)
        if action_type == "set_edge_capacity":
            if not str(target.get("edgeId") or ""):
                raise ContractError("set_edge_capacity requires edgeId")
            params["multiplier"] = self._number(params.get("multiplier"), "multiplier", 0.1, 1)
        if action_type == "set_edge_risk_weight":
            if not str(target.get("edgeId") or ""):
                raise ContractError("set_edge_risk_weight requires edgeId")
            params["weight"] = self._number(params.get("weight"), "weight", 0.1, 10)
        if action_type == "reroute_group":
            if not str(target.get("groupId") or "") or not isinstance(params.get("routeEdges"), list) or not params["routeEdges"]:
                raise ContractError("reroute_group requires groupId and routeEdges")
        if action_type == "set_route_distribution":
            if not str(target.get("originId") or ""):
                raise ContractError("set_route_distribution requires originId")
            values = params.get("distributions")
            if not isinstance(values, list) or not values:
                raise ContractError("route distributions are required")
            for item in values:
                if not isinstance(item, Mapping):
                    raise ContractError("each route distribution must be an object")
                if not str(item.get("routeId") or "").strip():
                    raise ContractError("each route distribution requires routeId")
                route_edges = item.get("routeEdges")
                if not isinstance(route_edges, list) or not route_edges or any(not str(edge).strip() for edge in route_edges):
                    raise ContractError("each route distribution requires non-empty routeEdges")
            total = sum(self._number(item.get("ratio"), "ratio", 0, 1) for item in values if isinstance(item, Mapping))
            if len(values) != sum(isinstance(item, Mapping) for item in values) or abs(total - 1) > 1e-5:
                raise ContractError("route distribution ratios must total 1")
        if action_type not in {"observe_only", "reroute_group"}:
            params["durationSeconds"] = self._number(params.get("durationSeconds", 120), "durationSeconds", 1, 1800)
