"""Gaussian PC / PC-stable causal discovery with explicit uncertainty.

The result is a CPDAG only if the skeleton search was not truncated and no
orientation conflicts occurred. Independent rows, causal sufficiency,
faithfulness, an acyclic target and a suitable Gaussian CI test are assumed.
"""

from __future__ import annotations

from itertools import combinations
import math
from collections.abc import Mapping, Sequence

from ._inference import finite, ordinary_least_squares


def _residuals(values: list[float], controls: list[list[float]]) -> list[float]:
    if not controls:
        center = sum(values) / len(values)
        return [value - center for value in values]
    design = [[1.0, *(column[row] for column in controls)] for row in range(len(values))]
    beta = ordinary_least_squares(design, values)
    return [value - sum(coefficient * item for coefficient, item in zip(beta, row))
            for value, row in zip(values, design)]


def fisher_z_ci(data: Sequence[Mapping[str, float]], source: str, target: str,
                conditioning: Sequence[str] = ()) -> dict:
    """Gaussian CI test on {source,target}∪conditioning only.

    Singular or constant samples fail explicitly; no ridge/pseudoinverse is
    inserted into the classical Fisher-z test.
    """
    n = len(data)
    keys = [source, target, *conditioning]
    if len(set(keys)) != len(keys):
        raise ValueError("CI variable names must be distinct")
    if n <= len(conditioning) + 3:
        raise ValueError("Fisher-z requires n > |S| + 3")
    columns = {key: [finite(row[key], key) for row in data] for key in keys}
    controls = [columns[key] for key in conditioning]
    left = _residuals(columns[source], controls)
    right = _residuals(columns[target], controls)
    numerator = sum(x * y for x, y in zip(left, right))
    denominator = math.sqrt(sum(x * x for x in left) * sum(y * y for y in right))
    if denominator <= 1e-12:
        raise ValueError("constant or singular CI variables")
    correlation = numerator / denominator
    if abs(correlation) >= 1 - 1e-12:
        raise ValueError("singular covariance: perfect conditional correlation")
    z = math.sqrt(n - len(conditioning) - 3) * math.atanh(correlation)
    p_value = math.erfc(abs(z) / math.sqrt(2))
    return {"partial_correlation": correlation, "z": z, "p_value": p_value,
            "sample_size": n, "conditioning": list(conditioning)}


def _edge(a: str, b: str) -> frozenset[str]:
    return frozenset((a, b))


def _reachable(source: str, target: str, directed: set[tuple[str, str]]) -> bool:
    frontier = [source]
    seen = set()
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(v for u, v in directed if u == current)
    return False


def _orient(variables: list[str], skeleton: dict[str, set[str]],
            separators: dict[frozenset[str], tuple[str, ...]]) -> tuple[set, set, list[dict]]:
    undirected = {_edge(a, b) for a in variables for b in skeleton[a] if a < b}
    directed: set[tuple[str, str]] = set()
    conflicts: list[dict] = []

    def apply(proposals: set[tuple[str, str]], rule: str) -> bool:
        changed = False
        for a, b in sorted(proposals):
            pair = _edge(a, b)
            if pair not in undirected:
                if (b, a) in directed:
                    conflicts.append({"edge": [a, b], "rule": rule, "reason": "opposite direction"})
                continue
            if (b, a) in proposals or _reachable(b, a, directed):
                conflicts.append({"edge": [a, b], "rule": rule, "reason": "opposed proposal or directed cycle"})
                continue
            undirected.remove(pair)
            directed.add((a, b))
            changed = True
        return changed

    # 无屏蔽三元组：若中点不在分离集中，提出碰撞点方向。
    colliders = set()
    for middle in variables:
        for left, right in combinations(sorted(skeleton[middle]), 2):
            if right in skeleton[left]:
                continue
            separator = separators.get(_edge(left, right))
            if separator is None:
                conflicts.append({"triple": [left, middle, right], "rule": "collider",
                                  "reason": "missing separating set"})
            elif middle not in separator:
                colliders.update(((left, middle), (right, middle)))
    apply(colliders, "collider")

    while True:
        proposals: set[tuple[str, str]] = set()
        # R1: a→b-c 且 a、c 不相邻，则 b→c。
        for a, b in directed:
            for c in skeleton[b] - {a}:
                if _edge(b, c) in undirected and c not in skeleton[a]:
                    proposals.add((b, c))
        # R2: a-b 且有有向路径 a→c→b，则 a→b。
        for pair in undirected:
            a, b = tuple(sorted(pair))
            for source, target in ((a, b), (b, a)):
                if any((source, middle) in directed and (middle, target) in directed
                       for middle in variables if middle not in (source, target)):
                    proposals.add((source, target))
        # R3: a-b、a-c、a-d 未定向，c→b、d→b，且 c、d 不相邻。
        for pair in undirected:
            a, b = tuple(sorted(pair))
            for source, target in ((a, b), (b, a)):
                parents = [node for node in skeleton[source] - {target}
                           if _edge(source, node) in undirected and (node, target) in directed]
                if any(right not in skeleton[left] for left, right in combinations(parents, 2)):
                    proposals.add((source, target))
        if not apply(proposals, "R1-R3"):
            break
    return undirected, directed, conflicts


def pc_discovery(
    data: Sequence[Mapping[str, float]], *, alpha: float = 0.05,
    max_condition_size: int | None = None, stable: bool = True,
) -> dict:
    """Discover a Gaussian PC or PC-stable skeleton, then orient basic CPDAG edges."""
    if not 0 < alpha < 1 or not data:
        raise ValueError("data must be nonempty and alpha in (0,1)")
    variables = list(data[0])
    if len(variables) < 2 or any(set(row) != set(variables) for row in data):
        raise ValueError("each independent row must contain the same >=2 variables")
    for key in variables:
        column = [finite(row[key], key) for row in data]
        if max(column) - min(column) <= 1e-12:
            raise ValueError(f"constant variable: {key}")
    if max_condition_size is not None and max_condition_size < 0:
        raise ValueError("max_condition_size must be nonnegative")
    adjacency = {key: set(variables) - {key} for key in variables}
    separators: dict[frozenset[str], tuple[str, ...]] = {}
    test_log = []
    level = 0
    truncated = False
    while True:
        snapshot = {key: set(neighbors) for key, neighbors in adjacency.items()} if stable else adjacency
        ordered_pairs = [(a, b) for a in variables for b in variables
                         if a != b and b in adjacency[a] and len(snapshot[a] - {b}) >= level]
        if not ordered_pairs:
            break
        if max_condition_size is not None and level > max_condition_size:
            truncated = True
            break
        for source, target in ordered_pairs:
            if target not in adjacency[source]:
                continue
            for chosen in combinations(sorted(snapshot[source] - {target}), level):
                try:
                    result = fisher_z_ci(data, source, target, chosen)
                    test_log.append({"source": source, "target": target, "level": level,
                                     "conditioning": list(chosen), "p_value": result["p_value"],
                                     "partial_correlation": result["partial_correlation"], "valid": True})
                except (ValueError, KeyError) as exc:
                    test_log.append({"source": source, "target": target, "level": level,
                                     "conditioning": list(chosen), "valid": False, "reason": str(exc)})
                    continue
                if result["p_value"] > alpha:
                    # p>alpha 仅表示未拒绝条件独立；据此删边并保存分离集。
                    adjacency[source].discard(target)
                    adjacency[target].discard(source)
                    separators[_edge(source, target)] = chosen
                    break
        level += 1
    undirected, directed, conflicts = _orient(variables, adjacency, separators)
    invalid_tests = [item for item in test_log if not item["valid"]]
    graph_type = "CPDAG" if not truncated and not conflicts and not invalid_tests else "uncertain_P(D)AG"
    return {"variables": variables,
            "skeleton": [list(sorted((a, b))) for a in variables for b in adjacency[a] if a < b],
            "undirected_edges": [list(sorted(pair)) for pair in sorted(undirected, key=lambda e: sorted(e))],
            "directed_edges": [list(pair) for pair in sorted(directed)],
            "separating_sets": {"|".join(sorted(pair)): list(value) for pair, value in separators.items()},
            "test_log": test_log, "invalid_test_count": len(invalid_tests),
            "conflicts": conflicts, "truncated": truncated, "stable_skeleton": stable,
            "graph_type": graph_type}
