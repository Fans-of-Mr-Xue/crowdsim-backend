"""Numeric primitives used by post-event graph extraction.

The frontend counterpart is postCrowdCausalGraphExtractor.js. A correlation
computed here is an association; it does not by itself establish causation.
"""

from __future__ import annotations

from itertools import combinations
import math
from collections.abc import Mapping, Sequence


EPSILON = 1e-9


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _series(values: Sequence[float], name: str) -> list[float]:
    return [_finite(value, name) for value in values]


def mean(values: Sequence[float]) -> float:
    numbers = _series(values, "values")
    if not numbers:
        raise ValueError("values must not be empty")
    return sum(numbers) / len(numbers)


def standard_deviation(values: Sequence[float]) -> float:
    """Population standard deviation, matching the post-event frontend."""
    numbers = _series(values, "values")
    if len(numbers) < 2:
        return 0.0
    center = mean(numbers)
    return math.sqrt(sum((value - center) ** 2 for value in numbers) / len(numbers))


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    """Return Pearson r, or NaN for fewer than two points/constant series."""
    left, right = _series(x, "x"), _series(y, "y")
    if len(left) != len(right):
        raise ValueError("x and y must have the same length")
    if len(left) < 2:
        return math.nan
    left_mean, right_mean = mean(left), mean(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    denominator = math.sqrt(
        sum(value * value for value in left_centered)
        * sum(value * value for value in right_centered)
    )
    if denominator <= EPSILON:
        return math.nan
    return sum(a * b for a, b in zip(left_centered, right_centered)) / denominator


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Solve normal equations with the frontend's 1e-6 diagonal regularizer."""
    size = len(vector)
    augmented = [
        [value + (1e-6 if row == column else 0.0) for column, value in enumerate(values)]
        + [vector[row]]
        for row, values in enumerate(matrix)
    ]
    for pivot in range(size):
        max_row = max(range(pivot, size), key=lambda row: abs(augmented[row][pivot]))
        if abs(augmented[max_row][pivot]) < EPSILON:
            return None
        augmented[pivot], augmented[max_row] = augmented[max_row], augmented[pivot]
        divisor = augmented[pivot][pivot]
        augmented[pivot] = [value / divisor for value in augmented[pivot]]
        for row in range(size):
            if row == pivot:
                continue
            factor = augmented[row][pivot]
            augmented[row] = [
                value - factor * augmented[pivot][column]
                for column, value in enumerate(augmented[row])
            ]
    return [row[-1] for row in augmented]


def residualize(values: Sequence[float], covariates: Sequence[Sequence[float]]) -> list[float]:
    """Remove the linear contribution of covariates using ridge-stabilized OLS."""
    target = _series(values, "values")
    controls = [_series(series, "covariate") for series in covariates]
    if any(len(series) != len(target) for series in controls):
        raise ValueError("covariates must have the same length as values")
    if not target:
        return []
    if not controls:
        center = mean(target)
        return [value - center for value in target]
    design = [[1.0, *(series[index] for series in controls)] for index in range(len(target))]
    width = len(controls) + 1
    xtx = [
        [sum(row[i] * row[j] for row in design) for j in range(width)]
        for i in range(width)
    ]
    xty = [sum(row[i] * value for row, value in zip(design, target)) for i in range(width)]
    coefficients = _solve_linear_system(xtx, xty)
    if coefficients is None:
        return residualize(target, [])
    return [
        value - sum(item * coefficient for item, coefficient in zip(row, coefficients))
        for value, row in zip(target, design)
    ]


def partial_correlation(
    x: Sequence[float], y: Sequence[float], covariates: Sequence[Sequence[float]] = ()
) -> float:
    """Pearson correlation after linearly removing the supplied covariates."""
    return pearson(residualize(x, covariates), residualize(y, covariates))


def best_partial_correlation(
    rows: Sequence[Mapping[str, float]],
    source: str,
    target: str,
    conditioning_keys: Sequence[str] = (),
    *,
    max_depth: int = 2,
) -> dict:
    """Select the conditioning set giving the weakest finite association.

    Mirrors the small-sample search in postCrowdCausalGraphExtractor.js.
    It is a screening heuristic, not a PC algorithm or causal identification.
    """
    if source == target:
        raise ValueError("source and target must differ")
    if max_depth < 0:
        raise ValueError("max_depth must not be negative")
    x = [_finite(row[source], source) for row in rows]
    y = [_finite(row[target], target) for row in rows]
    best: dict = {"effect": pearson(x, y), "conditioningSet": []}
    keys = list(dict.fromkeys(key for key in conditioning_keys if key not in {source, target}))
    depth_limit = min(max_depth, max(0, len(rows) - 4), len(keys))
    for depth in range(1, depth_limit + 1):
        for chosen in combinations(keys, depth):
            controls = [[_finite(row[key], key) for row in rows] for key in chosen]
            effect = partial_correlation(x, y, controls)
            if math.isfinite(effect) and (not math.isfinite(best["effect"]) or abs(effect) < abs(best["effect"])):
                best = {"effect": effect, "conditioningSet": list(chosen)}
    return best
