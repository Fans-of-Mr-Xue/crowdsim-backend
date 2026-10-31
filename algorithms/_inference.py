"""Shared numerical helpers; complete simulation runs are the resampling unit."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def finite(value: object, label: str = "value") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def series(values: Sequence[float], label: str = "values", *, minimum: int = 1) -> list[float]:
    result = [finite(value, label) for value in values]
    if len(result) < minimum:
        raise ValueError(f"{label} requires at least {minimum} independent runs")
    return result


def average(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def sample_variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        raise ValueError("sample variance requires at least two independent runs")
    center = average(values)
    return sum((value - center) ** 2 for value in values) / (len(values) - 1)


def percentile(values: Sequence[float], probability: float) -> float:
    if not 0 <= probability <= 1 or not values:
        raise ValueError("invalid percentile")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[min(lower + 1, len(ordered) - 1)] * fraction


def bootstrap_ci(
    values: Sequence[float], *, alpha: float = 0.05,
    repetitions: int = 2000, seed: int = 0,
) -> tuple[float, float]:
    """Percentile CI resampling whole independent runs, never time steps."""
    observations = series(values, minimum=2)
    if not 0 < alpha < 1 or repetitions < 100:
        raise ValueError("alpha must be in (0,1) and repetitions at least 100")
    rng = random.Random(seed)
    estimates = [
        average(rng.choices(observations, k=len(observations)))
        for _ in range(repetitions)
    ]
    return percentile(estimates, alpha / 2), percentile(estimates, 1 - alpha / 2)


def paired_summary(
    baseline: Sequence[float], candidate: Sequence[float], *,
    alpha: float = 0.05, repetitions: int = 2000, seed: int = 0,
) -> dict:
    left = series(baseline, "baseline", minimum=2)
    right = series(candidate, "candidate", minimum=2)
    if len(left) != len(right):
        raise ValueError("paired arms require the same number of aligned runs")
    differences = [new - old for old, new in zip(left, right)]
    interval = bootstrap_ci(differences, alpha=alpha, repetitions=repetitions, seed=seed)
    return {
        "effect": average(differences),
        "sd": math.sqrt(sample_variance(differences)),
        "se": math.sqrt(sample_variance(differences) / len(differences)),
        "ci": interval,
        "ci_method": "paired run-level percentile bootstrap",
        "n": len(differences),
        "differences": differences,
    }


def independent_summary(
    control: Sequence[float], treatment: Sequence[float], *,
    alpha: float = 0.05, repetitions: int = 2000, seed: int = 0,
) -> dict:
    left = series(control, "control", minimum=2)
    right = series(treatment, "treatment", minimum=2)
    if not 0 < alpha < 1 or repetitions < 100:
        raise ValueError("alpha must be in (0,1) and repetitions at least 100")
    rng = random.Random(seed)
    estimates = [
        average(rng.choices(right, k=len(right))) - average(rng.choices(left, k=len(left)))
        for _ in range(repetitions)
    ]
    return {
        "effect": average(right) - average(left),
        "se": math.sqrt(sample_variance(right) / len(right) + sample_variance(left) / len(left)),
        "ci": (percentile(estimates, alpha / 2), percentile(estimates, 1 - alpha / 2)),
        "ci_method": "independent arm percentile bootstrap",
        "n_control": len(left),
        "n_treatment": len(right),
    }


def solve(matrix: Sequence[Sequence[float]], vector: Sequence[float], *, tolerance: float = 1e-10) -> list[float]:
    """Strict Gaussian elimination; singular designs fail instead of using a pseudo-inverse."""
    size = len(vector)
    if not size or len(matrix) != size or any(len(row) != size for row in matrix):
        raise ValueError("linear system must be square")
    augmented = [list(map(float, row)) + [finite(vector[i])] for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= tolerance:
            raise ValueError("singular or aliased design matrix")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [value - factor * augmented[column][j] for j, value in enumerate(augmented[row])]
    return [row[-1] for row in augmented]


def ordinary_least_squares(design: Sequence[Sequence[float]], outcome: Sequence[float]) -> list[float]:
    response = series(outcome, "outcome")
    if len(design) != len(response) or not design or not design[0]:
        raise ValueError("design rows must align with outcome")
    width = len(design[0])
    if len(design) < width or any(len(row) != width for row in design):
        raise ValueError("design has insufficient rows or inconsistent width")
    rows = [[finite(value, "design value") for value in row] for row in design]
    gram = [[sum(row[i] * row[j] for row in rows) for j in range(width)] for i in range(width)]
    target = [sum(row[i] * y for row, y in zip(rows, response)) for i in range(width)]
    return solve(gram, target)
