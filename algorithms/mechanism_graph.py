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
    if 'rounds' in graph:
        return _round_key_nodes(graph, compute_round_plays(graph))
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
    if 'rounds' in graph:
        results = []
        for row in compute_round_plays(graph):
            for driver in row['drivers']:
                if driver.get('lag', 0) > 0:
                    results.append({'key': f"{row['id']}>{driver['nodeId']}", 'roundId': row['id'],
                        'sourceName': driver.get('nodeName'), 'targetName': row.get('name'),
                        'lag': driver['lag'], 'effect': driver['force'], 'kind': 'push' if driver['force'] >= 0 else 'drag'})
            if row.get('riskLag', 0) > 0:
                results.append({'key': row['id'] + '>risk', 'roundId': row['id'], 'sourceName': row.get('name'),
                    'targetName': graph.get('outcomeName'), 'lag': row['riskLag'], 'effect': row['riskDelta'], 'kind': 'risk'})
        return sorted(results, key=lambda item: (-item['lag'], -abs(item['effect'])))
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
    if 'rounds' in graph:
        plays = {row['id']: row for row in compute_round_plays(graph)}
        return [{**loop, 'rounds': [plays[key] for key in loop['roundIds'] if key in plays],
                 'riskDelta': sum(plays[key]['riskDelta'] for key in loop['roundIds'] if key in plays and plays[key]['played']),
                 'totalLag': sum(plays[key]['chainLag'] for key in loop['roundIds'] if key in plays)} for loop in graph.get('loops', [])]
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


def _round2(value):
    # Source uses Number(number.toFixed(2)), i.e. rounds the IEEE-754 value.
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal.from_float(_number(value, 'round value')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def compute_round_plays(graph: Mapping, threshold_override=None) -> list[dict]:
    """Source computeRoundPlays numeric logic, with supplied crowd input units.

    This engine consumes explicit drivers and thresholds. It never estimates
    them from correlations or treats an observation as a physical force.
    """
    risk = _number(graph['initialRisk'], 'initialRisk')
    plays = []
    for row in graph['rounds']:
        drivers = sorted([{**driver, 'force': _round2(_number(driver['effect'], 'effect') * _number(driver['weight'], 'weight'))}
                          for driver in row['drivers']], key=lambda item: -item['force'])
        pushers = [driver for driver in drivers if driver['force'] > 0]
        draggers = [driver for driver in drivers if driver['force'] < 0]
        push, drag = _round2(sum(d['force'] for d in pushers)), _round2(-sum(d['force'] for d in draggers))
        net = _round2(push - drag)
        threshold = _round2(row['threshold'] if threshold_override is None else threshold_override)
        played, before = net >= threshold, risk
        if played:
            risk = max(0, _round2(risk + _number(row['riskDelta'], 'riskDelta')))
        lag = max((d.get('lag', 0) for d in (pushers or drivers)), default=0)
        plays.append({**row, 'drivers': drivers, 'pushers': pushers, 'draggers': draggers,
            'push': push, 'drag': drag, 'net': net, 'threshold': threshold, 'played': played,
            'margin': _round2(net - threshold), 'riskBefore': before, 'riskAfter': risk,
            'leadDriver': (pushers or drivers or [None])[0], 'topDragger': draggers[-1] if draggers else None,
            'chainLag': lag + row.get('riskLag', 0)})
    return plays


def _round_key_nodes(graph, plays):
    """Source computeKeyNodes round branch, sharing the existing public entry."""
    scores, contributions, leads, appearances = {}, {}, {}, {}
    for node in graph.get('microNodes', []):
        scores[node['id']], contributions[node['id']], leads[node['id']], appearances[node['id']] = 0., 0., 0, []
    for edge in graph.get('microEdges', []):
        for key, weight in ((edge['source'], .6), (edge['target'], .4)):
            scores[key] = scores.get(key, 0) + edge['weight'] * weight
    for row in plays:
        for driver in row['drivers']:
            key = driver['nodeId']
            contributions[key] = contributions.get(key, 0) + abs(driver['force'])
            scores[key] = scores.get(key, 0) + abs(driver['force']) * 1.6
            if key in appearances:
                appearances[key].append({'roundId': row['id'], 'round': row['round'], 'force': driver['force'], 'mechanism': driver.get('mechanism')})
        if row['leadDriver']:
            key = row['leadDriver']['nodeId']
            scores[key] = scores.get(key, 0) + .4
            leads[key] = leads.get(key, 0) + 1
    return sorted([{'nodeId': node['id'], 'nodeName': node.get('name'), 'camp': node.get('camp'),
        'contribution': _round2(contributions[node['id']]), 'leadCount': leads[node['id']],
        'rounds': appearances[node['id']], 'score': _round2(scores[node['id']])} for node in graph.get('microNodes', [])], key=lambda item: -item['score'])
