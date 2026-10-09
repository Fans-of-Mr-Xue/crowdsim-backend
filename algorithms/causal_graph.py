"""Generic path, attribution, and intervention operations on a causal DAG.

Input and output field names follow causalAnalysisEngine.js so a later API
adapter can pass existing post-event graph data without changing page schemas.
Edge effects are supplied estimates; these functions do not infer them.
"""

from __future__ import annotations

from collections import deque
import math
from collections.abc import Mapping, Sequence


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _graph(nodes: Sequence[Mapping], edges: Sequence[Mapping]) -> tuple[dict, dict]:
    by_id: dict[str, Mapping] = {}
    for node in nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id or node_id in by_id:
            raise ValueError("node ids must be unique nonempty strings")
        by_id[node_id] = node
    adjacency: dict[str, list[Mapping]] = {node_id: [] for node_id in by_id}
    for edge in edges:
        source, target = edge.get("source"), edge.get("target")
        if source not in by_id or target not in by_id:
            raise ValueError("every edge endpoint must exist in nodes")
        _number(edge.get("effect"), "edge effect")
        adjacency[source].append(edge)
    return by_id, adjacency


def _paths(
    adjacency: Mapping[str, Sequence[Mapping]], source: str, target: str,
    max_depth: int, max_paths: int,
) -> list[tuple[list[str], list[Mapping]]]:
    paths: list[tuple[list[str], list[Mapping]]] = []

    def walk(node_id: str, trail: list[str], edge_trail: list[Mapping]) -> None:
        if node_id == target:
            if len(paths) >= max_paths:
                raise ValueError("graph has more paths than max_paths")
            paths.append((trail.copy(), edge_trail.copy()))
            return
        if len(trail) >= max_depth:
            return
        for edge in adjacency[node_id]:
            next_id = edge["target"]
            if next_id in trail:
                continue
            trail.append(next_id)
            edge_trail.append(edge)
            walk(next_id, trail, edge_trail)
            edge_trail.pop()
            trail.pop()

    walk(source, [source], [])
    return paths


def enumerate_risk_paths(
    nodes: Sequence[Mapping], edges: Sequence[Mapping], outcome_id: str,
    *, treatment_tier: str = "treatment", max_depth: int = 6,
    max_paths: int = 10_000,
) -> list[dict]:
    """Enumerate simple treatment-to-outcome paths and multiply edge effects.

    ``max_depth`` counts nodes as in the frontend. Confidence is the product
    of edge confidences; it is a display heuristic, not a calibrated probability.
    """
    by_id, adjacency = _graph(nodes, edges)
    if outcome_id not in by_id:
        raise ValueError("outcome_id must exist in nodes")
    if max_depth < 2 or max_paths < 1:
        raise ValueError("max_depth must be at least 2 and max_paths positive")
    results = []
    for node in nodes:
        source = node["id"]
        if node.get("tier") != treatment_tier or source == outcome_id:
            continue
        for trail, path_edges in _paths(adjacency, source, outcome_id, max_depth, max_paths - len(results)):
            effect = math.prod(_number(edge["effect"], "edge effect") for edge in path_edges)
            confidence = math.prod(_number(edge.get("confidence", 1), "edge confidence") for edge in path_edges)
            results.append({
                "id": ">".join(trail),
                "sourceId": source,
                "nodeIds": trail,
                "nodeNames": [by_id[node_id].get("name") or node_id for node_id in trail],
                "edgeKeys": [f'{edge["source"]}>{edge["target"]}' for edge in path_edges],
                "effect": round(effect, 3),
                "confidence": round(confidence, 2),
                "verified": all(bool(edge.get("verified")) for edge in path_edges),
            })
    results.sort(key=lambda path: abs(path["effect"]), reverse=True)
    return results


def compute_leverages(nodes: Sequence[Mapping], edges: Sequence[Mapping], outcome_id: str) -> list[dict]:
    """Sum each treatment node's path effects on the selected outcome."""
    by_id = {node["id"]: node for node in nodes}
    totals: dict[str, dict] = {}
    for path in enumerate_risk_paths(nodes, edges, outcome_id):
        entry = totals.setdefault(path["sourceId"], {"totalEffect": 0.0, "pathCount": 0})
        entry["totalEffect"] += path["effect"]
        entry["pathCount"] += 1
    results = [
        {
            "nodeId": node_id,
            "nodeName": by_id[node_id].get("name") or node_id,
            "totalEffect": round(entry["totalEffect"], 3),
            "pathCount": entry["pathCount"],
        }
        for node_id, entry in totals.items()
    ]
    results.sort(key=lambda item: abs(item["totalEffect"]), reverse=True)
    return results


def simulate_intervention(
    nodes: Sequence[Mapping], edges: Sequence[Mapping], interventions: Mapping | Sequence[Mapping],
    outcome_id: str, baseline: float, *, projected_min: float = 5,
    projected_max: float = 98,
) -> dict:
    """Propagate additive do-deltas over a DAG using supplied linear effects."""
    by_id, adjacency = _graph(nodes, edges)
    if outcome_id not in by_id:
        raise ValueError("outcome_id must exist in nodes")
    base = _number(baseline, "baseline")
    lower, upper = _number(projected_min, "projected_min"), _number(projected_max, "projected_max")
    if lower > upper:
        raise ValueError("projected_min must not exceed projected_max")
    selected = [interventions] if isinstance(interventions, Mapping) else list(interventions)
    deltas: dict[str, float] = {}
    for intervention in selected:
        node_id = intervention.get("nodeId")
        if node_id not in by_id:
            raise ValueError("intervention nodeId must exist in nodes")
        deltas[node_id] = deltas.get(node_id, 0.0) + _number(intervention.get("delta"), "delta")
    indegree = {node_id: 0 for node_id in by_id}
    for edge in edges:
        indegree[edge["target"]] += 1
    queue = deque(node_id for node_id in by_id if indegree[node_id] == 0)
    visited = 0
    while queue:
        node_id = queue.popleft()
        visited += 1
        value = deltas.get(node_id, 0.0)
        for edge in adjacency[node_id]:
            target = edge["target"]
            if value:
                deltas[target] = deltas.get(target, 0.0) + value * _number(edge["effect"], "edge effect")
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(by_id):
        raise ValueError("intervention propagation requires a directed acyclic graph")
    outcome_delta = deltas.get(outcome_id, 0.0)
    projected = min(upper, max(lower, math.floor(base * (1 + outcome_delta) + 0.5)))
    return {
        "baseline": base,
        "projected": projected,
        "deltaPct": math.floor(outcome_delta * 100 + 0.5),
        "nodeDeltas": [
            {"nodeId": node_id, "nodeName": by_id[node_id].get("name") or node_id, "delta": round(value, 3)}
            for node_id, value in deltas.items()
        ],
    }


def find_hub_nodes(
    nodes: Sequence[Mapping], edges: Sequence[Mapping], outcome_id: str,
    *, mediator_tier: str = "mediator", min_path_count: int = 2,
) -> list[dict]:
    """Rank mediators traversed by at least ``min_path_count`` risk paths."""
    by_id = {node["id"]: node for node in nodes}
    counts: dict[str, int] = {}
    for path in enumerate_risk_paths(nodes, edges, outcome_id):
        for node_id in path["nodeIds"][1:-1]:
            if by_id[node_id].get("tier") == mediator_tier:
                counts[node_id] = counts.get(node_id, 0) + 1
    results = [
        {"nodeId": node_id, "nodeName": by_id[node_id].get("name") or node_id, "pathCount": count}
        for node_id, count in counts.items() if count >= min_path_count
    ]
    results.sort(key=lambda item: item["pathCount"], reverse=True)
    return results
