"""Orthogonal-array balance checks and estimable-factor analysis (method B3)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from itertools import product

from ._inference import bootstrap_ci, finite, ordinary_least_squares


def orthogonal_design_analysis(
    design: Sequence[Mapping[str, object]], outcomes: Sequence[Sequence[float]], *,
    interactions: Sequence[tuple[str, str]] = (), alpha: float = 0.05,
    seed: int = 0,
) -> dict:
    """Analyse an externally selected OA; never invent a design table.

    ``outcomes[d][r]`` is one full-run response for design row d and block r.
    Treatment dummy coding uses the last level as reference. Requested
    interaction terms must be full-rank or the analysis is rejected.
    """
    if not design or len(design) != len(outcomes):
        raise ValueError("design and outcomes must contain aligned rows")
    factors = list(design[0])
    if len(factors) < 2 or any(set(row) != set(factors) for row in design):
        raise ValueError("each design row must define the same two or more factors")
    repeats = {len(row) for row in outcomes}
    if len(repeats) != 1 or next(iter(repeats)) < 2:
        raise ValueError("each design row needs at least two aligned independent blocks")
    values = [[finite(value, "outcome") for value in row] for row in outcomes]
    levels = {factor: list(dict.fromkeys(row[factor] for row in design)) for factor in factors}
    if any(len(items) < 2 for items in levels.values()):
        raise ValueError("each factor must vary across at least two levels")

    # OA 强度 2 要求任意两因素的每个水平组合出现相同次数。
    for index, left in enumerate(factors):
        for right in factors[index + 1:]:
            counts = Counter((row[left], row[right]) for row in design)
            expected = len(design) / (len(levels[left]) * len(levels[right]))
            if not expected.is_integer() or any(counts[pair] != expected
                                                for pair in product(levels[left], levels[right])):
                raise ValueError(f"factor pair {left}/{right} is not pairwise balanced")

    dummy_names = {}
    for factor in factors:
        dummy_names[factor] = [(factor, level) for level in levels[factor][:-1]]
    columns = ["intercept"]
    columns += [f"{factor}={level!r}" for factor in factors for _, level in dummy_names[factor]]
    for left, right in interactions:
        if left not in levels or right not in levels or left == right:
            raise ValueError("interaction must name two distinct design factors")
        columns += [f"{left}={a!r}*{right}={b!r}"
                    for _, a in dummy_names[left] for _, b in dummy_names[right]]

    matrix = []
    for row in design:
        vector = [1.0]
        vector += [float(row[factor] == level)
                   for factor in factors for _, level in dummy_names[factor]]
        for left, right in interactions:
            vector += [float(row[left] == a and row[right] == b)
                       for _, a in dummy_names[left] for _, b in dummy_names[right]]
        matrix.append(vector)
    if len(matrix) < len(columns):
        raise ValueError("requested model has more columns than design rows")

    # 每个独立重复区组分别拟合，设计混杂或秩亏时立即失败。
    coefficients_by_run = [ordinary_least_squares(matrix, [row[r] for row in values])
                           for r in range(next(iter(repeats)))]
    coefficients = {}
    for column, name in enumerate(columns):
        observations = [row[column] for row in coefficients_by_run]
        coefficients[name] = {"estimate": sum(observations) / len(observations),
                              "ci": bootstrap_ci(observations, alpha=alpha, seed=seed)}
    level_means = {}
    for factor in factors:
        level_means[factor] = {
            str(level): sum(sum(values[i]) / len(values[i]) for i, row in enumerate(design)
                            if row[factor] == level) / sum(row[factor] == level for row in design)
            for level in levels[factor]
        }
    ranges = {factor: max(means.values()) - min(means.values())
              for factor, means in level_means.items()}
    return {"factors": factors, "levels": levels, "columns": columns,
            "coefficients": coefficients, "level_means": level_means,
            "descriptive_ranges": ranges, "pairwise_balanced": True,
            "interactions_requested": list(interactions),
            "alias_warning": "未纳入的高阶交互可能与已估主效应混杂；需核查原设计的别名结构。"}
