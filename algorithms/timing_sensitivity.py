"""Intervention-time scan and local finite differences (method A3)."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence

from ._inference import finite, paired_summary


def timing_sensitivity(
    simulate: Callable[[object, object], Mapping[str, float]],
    baseline_policy: object, policy_at: Callable[[float], object],
    times: Sequence[float], streams: Sequence[object], metric_name: str, *,
    alpha: float = 0.05, seed: int = 0,
    validation_streams: Sequence[object] | None = None,
    direction: str | None = None,
) -> dict:
    """Estimate paired effects at each feasible start time.

    ``validation_streams`` should be independent from the discovery streams.
    The simulator must preserve the common pre-intervention history itself.
    """
    grid = sorted({finite(time, "time") for time in times})
    if not grid or len(streams) < 2:
        raise ValueError("provide a timing grid and at least two independent streams")
    if direction not in (None, "up", "down"):
        raise ValueError("direction must be up, down, or None")
    baseline_runs = []
    candidates = {time: [] for time in grid}
    for stream in streams:
        baseline_runs.append(finite(simulate(deepcopy(baseline_policy), deepcopy(stream))[metric_name], metric_name))
        for time in grid:
            candidates[time].append(finite(simulate(policy_at(time), deepcopy(stream))[metric_name], metric_name))
    rows = []
    for index, time in enumerate(grid):
        effect = paired_summary(baseline_runs, candidates[time], alpha=alpha, seed=seed)
        derivative = None
        if 0 < index < len(grid) - 1:
            left, right = grid[index - 1], grid[index + 1]
            derivative = ((sum(candidates[right]) / len(streams))
                          - (sum(candidates[left]) / len(streams))) / (right - left)
        rows.append({"time": time, "effect": effect, "timing_derivative": derivative})
    validation = None
    if validation_streams is not None:
        if len(validation_streams) < 2:
            raise ValueError("validation needs at least two fresh independent streams")
        if direction is None:
            raise ValueError("direction is required when selecting a timing for validation")
        # 数据内最优时点须在新随机流上复核，避免选择后高估。
        selected = (min if direction == "down" else max)(
            rows, key=lambda row: row["effect"]["effect"])["time"]
        old = [finite(simulate(deepcopy(baseline_policy), deepcopy(stream))[metric_name], metric_name)
               for stream in validation_streams]
        new = [finite(simulate(policy_at(selected), deepcopy(stream))[metric_name], metric_name)
               for stream in validation_streams]
        validation = {"selected_time": selected,
                      "effect": paired_summary(old, new, alpha=alpha, seed=seed + 1)}
    return {"metric": metric_name, "direction": direction, "times": rows, "validation": validation,
            "pointwise_ci": True}
