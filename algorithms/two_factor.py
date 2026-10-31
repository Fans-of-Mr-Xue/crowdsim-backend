"""Paired 2x2 full-factorial main and interaction contrasts (method B2)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ._inference import bootstrap_ci, finite, sample_variance


def two_factor_effects(
    cells: Mapping[tuple[int, int], Sequence[float]], *,
    alpha: float = 0.05, seed: int = 0,
) -> dict:
    """Four cells must align by independent repetition/random stream."""
    required = {(0, 0), (1, 0), (0, 1), (1, 1)}
    if set(cells) != required:
        raise ValueError("all four 2x2 cells are required")
    counts = {len(values) for values in cells.values()}
    if len(counts) != 1 or next(iter(counts)) < 2:
        raise ValueError("all cells need at least two aligned repetitions")
    values = {key: [finite(value) for value in observations] for key, observations in cells.items()}
    effects = {"A": [], "B": [], "interaction_J": []}
    for y00, y10, y01, y11 in zip(values[(0, 0)], values[(1, 0)], values[(0, 1)], values[(1, 1)]):
        effects["A"].append((y10 + y11 - y00 - y01) / 2)
        effects["B"].append((y01 + y11 - y00 - y10) / 2)
        effects["interaction_J"].append(y11 - y10 - y01 + y00)
    result = {}
    for key, observations in effects.items():
        result[key] = {"estimate": sum(observations) / len(observations),
                       "se": (sample_variance(observations) / len(observations)) ** 0.5,
                       "ci": bootstrap_ci(observations, alpha=alpha, seed=seed),
                       "per_run": observations}
    # ±1 编码的 DOE 交互效应等于 J/2，避免混淆两种口径。
    result["doe_interaction_effect"] = result["interaction_J"]["estimate"] / 2
    return result
