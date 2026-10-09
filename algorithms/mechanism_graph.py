"""Reusable micro/macro mechanism graph summaries.

Fields and weights follow the existing mechanismAnalysisEngine.js. This
module consumes an existing graph; it does not discover causal structure.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


EDGE_KIND_LABEL = {
    "micro": "微观交互",
    "aggregation": "聚合(向上)",
    "feedback": "反馈(向下)",
    "macro": "宏观传导",
}
EDGE_GROUPS = ("microEdges", "aggregationEdges", "feedbackEdges", "macroEdges")


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _edges(graph: Mapping) -> list[Mapping]:
    return [edge for group in EDGE_GROUPS for edge in graph.get(group, [])]


def _names(graph: Mapping) -> dict[str, str]:
    return {
        node["id"]: node.get("name") or node["id"]
        for node in [*graph.get("microNodes", []), *graph.get("macroNodes", [])]
    }


def compute_key_nodes(graph: Mapping) -> list[dict]:
    """Rank micro nodes by weighted degree and macro aggregation contribution."""
    nodes = graph.get("microNodes", [])
    names = _names(graph)
    scores = {node["id"]: 0.0 for node in nodes}
    degrees = {node["id"]: 0 for node in nodes}
    contributions = {node["id"]: 0.0 for node in nodes}

    for edge in graph.get("microEdges", []):
        source, target = edge["source"], edge["target"]
        weight = _number(edge["weight"], "micro edge weight")
        for node_id in (source, target):
            if node_id in degrees:
                degrees[node_id] += 1
        if source in scores:
            scores[source] += weight * 0.6
        if target in scores:
            scores[target] += weight * 0.4
    for edge in graph.get("aggregationEdges", []):
        source = edge["source"]
        contribution = abs(_number(edge["effect"], "aggregation effect")) * _number(edge["weight"], "aggregation weight")
        if source in scores:
            contributions[source] += contribution
            scores[source] += contribution * 1.6
    for edge in graph.get("feedbackEdges", []):
        target = edge["target"]
        if target in scores:
            scores[target] += _number(edge["weight"], "feedback weight") * 0.5

    results = [
        {
            "nodeId": node["id"],
            "nodeName": names[node["id"]],
            "camp": node.get("camp"),
            "influence": node.get("influence"),
            "degree": degrees[node["id"]],
            "aggContribution": round(contributions[node["id"]], 2),
            "score": round(scores[node["id"]], 2),
        }
        for node in nodes
    ]
    results.sort(key=lambda item: item["score"], reverse=True)
    return results


def list_lag_effects(graph: Mapping) -> list[dict]:
    """List delayed edges, descending by lag and then absolute effect."""
    names = _names(graph)
    results = []
    for edge in _edges(graph):
        lag = _number(edge.get("lag", 0), "edge lag")
        if lag <= 0:
            continue
        source, target = edge["source"], edge["target"]
        kind = edge.get("kind")
        results.append({
            "key": f"{source}>{target}",
            "source": source,
            "target": target,
            "sourceName": names.get(source, source),
            "targetName": names.get(target, target),
            "lag": lag,
            "effect": _number(edge.get("effect", 0), "edge effect"),
            "kind": kind,
            "kindLabel": EDGE_KIND_LABEL.get(kind, kind),
        })
    results.sort(key=lambda item: (item["lag"], abs(item["effect"])), reverse=True)
    return results


def format_loops(graph: Mapping) -> list[dict]:
    """Resolve configured loop node paths to edges for highlighting/reporting."""
    names = _names(graph)
    edge_keys = {(edge["source"], edge["target"]): f'{edge["source"]}>{edge["target"]}' for edge in _edges(graph)}
    results = []
    for loop in graph.get("loops", []):
        path: Sequence[str] = loop["nodePath"]
        keys = [edge_keys[pair] for pair in zip(path, path[1:]) if pair in edge_keys]
        results.append({
            "id": loop["id"],
            "type": loop["type"],
            "typeLabel": "增强回路 R" if loop["type"] == "R" else "平衡回路 B",
            "nodeIds": list(dict.fromkeys(path)),
            "nodeNames": loop.get("nodeNames") or [names.get(node_id, node_id) for node_id in path],
            "edgeKeys": keys,
            "totalLag": loop.get("totalLag"),
            "desc": loop.get("desc"),
        })
    return results
