"""Morris elementary-effect screening (optional OFAT extension)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from ._inference import finite, sample_variance


def morris_elementary_effects(
    transitions: Sequence[Mapping],
) -> dict[str, dict]:
    """Summarize one-factor moves from independent randomized trajectories.

    Each transition needs factor, baseline_output, changed_output and delta.
    Factor ranges/trajectory construction are supplied by the caller.
    """
    grouped: dict[str, list[float]] = {}
    for transition in transitions:
        name = str(transition["factor"])
        delta = finite(transition["delta"], "delta")
        if delta == 0:
            raise ValueError("Morris step must be nonzero")
        effect = (finite(transition["changed_output"], "changed_output")
                  - finite(transition["baseline_output"], "baseline_output")) / delta
        grouped.setdefault(name, []).append(effect)
    results = {}
    for factor, effects in grouped.items():
        # μ* 用绝对效应筛选重要因素，σ反映非线性或交互的可能性。
        results[factor] = {"mu": sum(effects) / len(effects),
                           "mu_star": sum(map(abs, effects)) / len(effects),
                           "sigma": math.sqrt(sample_variance(effects)) if len(effects) > 1 else None,
                           "elementary_effects": effects}
    return results
