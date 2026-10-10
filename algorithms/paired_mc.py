"""Common-random-number paired Monte Carlo comparison (method A2)."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence

from ._inference import finite, paired_summary, sample_variance


def paired_monte_carlo(
    simulate: Callable[[object, object], Mapping[str, float]],
    baseline_policy: object, candidate_policy: object,
    streams: Sequence[object], metric_names: Sequence[str], *,
    alpha: float = 0.05, seed: int = 0,
) -> dict:
    """Run both policies on separately copied versions of each aligned stream."""
    if len(streams) < 2 or not metric_names:
        raise ValueError("at least two independent streams and one metric are required")
    pairs, failures = [], []
    for index, stream in enumerate(streams):
        try:
            # 两臂共享同一外生输入，但互不修改对方的随机流对象。
            baseline = simulate(deepcopy(baseline_policy), deepcopy(stream))
            candidate = simulate(deepcopy(candidate_policy), deepcopy(stream))
            pairs.append({"run_index": index,
                          "baseline": {key: finite(baseline[key], key) for key in metric_names},
                          "candidate": {key: finite(candidate[key], key) for key in metric_names}})
        except Exception as exc:
            failures.append({"run_index": index, "error": str(exc)})
    if failures:
        return {"status": "incomplete", "pairs": pairs, "failures": failures, "effects": None}
    effects = {}
    for name in metric_names:
        old = [pair["baseline"][name] for pair in pairs]
        new = [pair["candidate"][name] for pair in pairs]
        summary = paired_summary(old, new, alpha=alpha, seed=seed)
        # 共同随机数并非必然降方差，显式报告对照值。
        summary["variance_of_difference"] = sample_variance(summary["differences"])
        summary["independent_variance_reference"] = sample_variance(old) + sample_variance(new)
        summary["variance_reduced"] = summary["variance_of_difference"] < summary["independent_variance_reference"]
        effects[name] = summary
    return {"status": "complete", "pairs": pairs, "failures": [], "effects": effects}
