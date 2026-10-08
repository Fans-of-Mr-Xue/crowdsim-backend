"""R1-R6 route decision service over CrowdSim-validated route candidates."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

from .llm_client import ControlLlmClient


class RoutingService:
    METHOD_MAP = {
        "R1": "dijkstra",
        "R2": "risk_aware_astar",
        "R3": "min_cost_flow",
        "R4": "dynamic_traffic_assignment",
    }

    def __init__(self, gateway, *, llm_client: ControlLlmClient | None = None) -> None:
        self.gateway = gateway
        self.llm_client = llm_client or ControlLlmClient()

    def evaluate(self, method_id: str, payload: Mapping[str, Any], *, user_id: str | None = None) -> dict[str, Any]:
        data = dict(payload)
        routes = data.get("routes")
        routes = self._validate_routes(routes)
        if method_id in self.METHOD_MAP:
            raw_parameters = dict(data.get("parameters") or {})
            aliases = {
                "riskWeight": "risk_weight",
                "congestionAlpha": "congestion_alpha",
                "congestionBeta": "congestion_beta",
            }
            parameters = {aliases.get(key, key): value for key, value in raw_parameters.items()}
            if method_id in {"R3", "R4"}:
                parameters.setdefault("demand", float(data.get("demand", 0.0)))
            allowed_parameters = {
                "R1": set(),
                "R2": {"risk_weight"},
                "R3": {"demand", "risk_weight"},
                "R4": {"demand", "iterations", "congestion_alpha", "congestion_beta"},
            }[method_id]
            parameters = {key: value for key, value in parameters.items() if key in allowed_parameters}
            request_id = self.gateway.submit({
                "action": "evaluate_routes",
                "algorithm": self.METHOD_MAP[method_id],
                "routes": routes,
                "parameters": parameters,
            })
            result = self.gateway.wait_for_ack(request_id, timeout=float(data.get("timeoutSeconds", 30)))
            if not result or result.get("type") == "error":
                raise RuntimeError(f"CrowdSim routing evaluation failed: {result}")
            return result["result"]
        if method_id == "R5":
            self._validate_topology(routes, timeout=float(data.get("timeoutSeconds", 30)))
            return self._llm_routing(routes, data, user_id=user_id)
        if method_id == "R6":
            self._validate_topology(routes, timeout=float(data.get("timeoutSeconds", 30)))
            return self._arde_routing(routes, float(data.get("demand", 1.0)))
        raise ValueError(f"unknown routing method: {method_id}")

    def _validate_topology(self, routes: list[dict[str, Any]], *, timeout: float) -> None:
        request_id = self.gateway.submit({"action": "evaluate_routes", "algorithm": "validate_only", "routes": routes})
        result = self.gateway.wait_for_ack(request_id, timeout=timeout)
        if not result or result.get("type") == "error" or not (result.get("result") or {}).get("valid"):
            raise ValueError(f"CrowdSim route topology validation failed: {result}")

    @staticmethod
    def _validate_routes(routes) -> list[dict[str, Any]]:
        if not isinstance(routes, list) or not routes:
            raise ValueError("routes must be a non-empty array")
        validated = []
        route_ids = set()
        for route in routes:
            if not isinstance(route, Mapping):
                raise ValueError("each route must be an object")
            item = dict(route)
            route_id = str(item.get("routeId") or item.get("id") or "").strip()
            edges = item.get("routeEdges") or item.get("edges")
            if not route_id or route_id in route_ids:
                raise ValueError("route ids must be non-empty and unique")
            if not isinstance(edges, list) or not edges or any(not str(edge).strip() for edge in edges):
                raise ValueError(f"route {route_id} requires non-empty routeEdges")
            for field, default in (("length", item.get("cost", 0.0)), ("risk", 0.0), ("capacity", math.inf)):
                value = float(item.get(field, default))
                invalid_finite = field != "capacity" and not math.isfinite(value)
                if invalid_finite or math.isnan(value) or value < 0 or (field == "capacity" and value <= 0):
                    raise ValueError(f"route {route_id} has invalid {field}")
            route_ids.add(route_id)
            validated.append(item)
        return validated

    def compare(self, payload: Mapping[str, Any], *, user_id: str | None = None) -> dict[str, Any]:
        """Evaluate the requested R1-R6 methods over one immutable input snapshot."""
        data = dict(payload)
        method_ids = data.get("methodIds") or ["R1", "R2", "R3", "R4", "R5", "R6"]
        if not isinstance(method_ids, list) or not method_ids:
            raise ValueError("methodIds must be a non-empty array")
        unknown = [method_id for method_id in method_ids if method_id not in {*self.METHOD_MAP, "R5", "R6"}]
        if unknown:
            raise ValueError(f"unknown routing methods: {unknown}")
        results: dict[str, Any] = {}
        for method_id in method_ids:
            try:
                results[method_id] = {"status": "completed", "result": self.evaluate(method_id, data, user_id=user_id)}
            except Exception as exc:
                results[method_id] = {"status": "failed", "error": str(exc)}
        return {
            "schemaVersion": "1.0",
            "methodIds": list(method_ids),
            "input": {
                "demand": data.get("demand"),
                "routes": data.get("routes"),
                "parameters": dict(data.get("parameters") or {}),
            },
            "results": results,
        }

    @staticmethod
    def _arde_routing(routes: list[dict[str, Any]], demand: float) -> dict[str, Any]:
        if not math.isfinite(demand) or demand <= 0:
            raise ValueError("ARDE routing demand must be positive and finite")
        weighted = []
        for route in routes:
            cost = max(0.001, float(route.get("length", route.get("cost", 0.0))) + 2.0 * float(route.get("risk", 0.0)))
            capacity = max(0.0, float(route.get("capacity", demand)))
            weighted.append((route, capacity, 1.0 / cost))
        if sum(capacity for _, capacity, _ in weighted) + 1e-9 < demand:
            raise ValueError("route capacity is insufficient for ARDE demand")
        if sum(weight for _, _, weight in weighted) <= 0:
            raise ValueError("routes have no usable ARDE weight")
        flows = [0.0 for _ in weighted]
        active = set(range(len(weighted)))
        remaining = demand
        while active and remaining > 1e-9:
            total_weight = sum(weighted[index][2] for index in active)
            saturated = []
            for index in active:
                share = remaining * weighted[index][2] / total_weight
                available = weighted[index][1] - flows[index]
                if share >= available - 1e-9:
                    flows[index] += max(0.0, available)
                    saturated.append(index)
            if not saturated:
                for index in active:
                    flows[index] += remaining * weighted[index][2] / total_weight
                remaining = 0.0
                break
            for index in saturated:
                active.remove(index)
            remaining = demand - sum(flows)
        allocations = []
        assigned_ratio = 0.0
        for index, (route, _, _) in enumerate(weighted):
            ratio = 1.0 - assigned_ratio if index == len(weighted) - 1 else flows[index] / demand
            assigned_ratio += ratio
            allocations.append({
                "routeId": str(route.get("routeId") or route.get("id")),
                "routeEdges": list(route.get("routeEdges") or route.get("edges") or []),
                "ratio": ratio,
                "flow": demand * ratio,
            })
        return {"algorithm": "arde_routing", "demand": demand, "allocations": allocations}

    def _llm_routing(self, routes, data, *, user_id=None):
        prompt = {
            "task": "Allocate route ratios. Use only supplied routeId values. Ratios must total 1.",
            "routes": routes,
            "demand": data.get("demand"),
        }
        text, metadata = self.llm_client.complete(
            [
                {"role": "system", "content": "Return JSON only: {\"allocations\":[{\"routeId\":...,\"ratio\":...}]}"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            timeout=float(data.get("timeoutSeconds", 30)),
            model=data.get("model"),
            max_tokens=int(data.get("maxTokens", 1200)),
        )
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("LLM routing response did not contain a JSON object")
        result = json.loads(text[start:end + 1])
        allocations = result.get("allocations")
        allowed = {str(route.get("routeId") or route.get("id")): route for route in routes}
        if not isinstance(allocations, list) or any(str(item.get("routeId")) not in allowed for item in allocations):
            raise ValueError("LLM returned invalid route ids")
        ratios = [float(item.get("ratio", 0)) for item in allocations]
        if any(not math.isfinite(ratio) or not 0 <= ratio <= 1 for ratio in ratios):
            raise ValueError("LLM route ratios must be in [0, 1]")
        if len({str(item.get("routeId")) for item in allocations}) != len(allocations):
            raise ValueError("LLM route ids must be unique")
        total = sum(ratios)
        if abs(total - 1.0) > 1e-5:
            raise ValueError("LLM route ratios must total 1")
        demand = float(data.get("demand", 1.0))
        if not math.isfinite(demand) or demand <= 0:
            raise ValueError("LLM routing demand must be positive and finite")
        for item in allocations:
            route = allowed[str(item["routeId"])]
            if demand * float(item["ratio"]) > float(route.get("capacity", math.inf)) + 1e-9:
                raise ValueError(f"LLM allocation exceeds capacity for route {item['routeId']}")
        return {
            "algorithm": "llm_routing",
            "allocations": [{
                **item,
                "routeEdges": list(allowed[str(item["routeId"])].get("routeEdges") or allowed[str(item["routeId"])].get("edges") or []),
                "flow": demand * float(item["ratio"]),
            } for item in allocations],
            "model": metadata.get("model") or data.get("model"),
        }
