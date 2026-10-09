"""One-factor-at-a-time scan around a fixed baseline (method B1)."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence

from ._inference import finite, paired_summary


def ofat_scan(
    simulate: Callable[[Mapping, object], Mapping[str, float]],
    baseline_config: Mapping, factor_levels: Mapping[str, Sequence[object]],
    streams: Sequence[object], metric_names: Sequence[str], *,
    alpha: float = 0.05, seed: int = 0,
) -> dict:
    """Vary one factor only; all other parameters and paired streams stay fixed."""
    if len(streams) < 2 or not factor_levels or not metric_names:
        raise ValueError("provide factors, metrics, and at least two independent streams")
    baseline = [simulate(deepcopy(baseline_config), deepcopy(stream)) for stream in streams]
    rows = []
    for factor, levels in factor_levels.items():
        if factor not in baseline_config or not levels:
            raise ValueError(f"unknown factor or empty levels: {factor}")
        for level in levels:
            config = deepcopy(dict(baseline_config))
            config[factor] = level
            runs = [simulate(deepcopy(config), deepcopy(stream)) for stream in streams]
            effects = {
                name: paired_summary(
                    [finite(row[name], name) for row in baseline],
                    [finite(row[name], name) for row in runs], alpha=alpha, seed=seed)
                for name in metric_names
            }
            rows.append({"factor": factor, "level": level, "effects": effects})
    # OFAT 只报告基线附近的单因素效应，不推断因素交互。
    return {"baseline_config": dict(baseline_config), "rows": rows,
            "interaction_estimated": False}


def local_derivative(lower_mean: float, upper_mean: float, lower_level: float, upper_level: float) -> float:
    low, high = finite(lower_level), finite(upper_level)
    if high <= low:
        raise ValueError("upper_level must exceed lower_level")
    return (finite(upper_mean) - finite(lower_mean)) / (high - low)
