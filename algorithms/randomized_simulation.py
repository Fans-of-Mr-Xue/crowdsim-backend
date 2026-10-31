"""Independent-run randomized comparison in a simulator (method A1)."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence

from ._inference import finite, independent_summary


def randomized_simulation(
    units: Sequence[object], simulate: Callable[[object, int, int], Mapping[str, float]],
    metric_names: Sequence[str], *, seed: int = 0, alpha: float = 0.05,
) -> dict:
    """Randomly assign independent *scenario runs* to 0/1 treatment.

    ``simulate(unit, treatment, independent_seed)`` must return endpoint values.
    This is a simulated randomized experiment, not a real-world human RCT.
    """
    if len(units) < 4 or not metric_names:
        raise ValueError("at least four independent units and one metric are required")
    rng = random.Random(seed)
    assignment = [0] * (len(units) // 2) + [1] * (len(units) - len(units) // 2)
    rng.shuffle(assignment)
    order = list(range(len(units)))
    rng.shuffle(order)
    records = []
    failures = []
    for index in order:
        run_seed = rng.getrandbits(64)
        try:
            output = simulate(units[index], assignment[index], run_seed)
            values = {name: finite(output[name], name) for name in metric_names}
            records.append({"unit_index": index, "treatment": assignment[index],
                            "seed": run_seed, "metrics": values})
        except Exception as exc:
            failures.append({"unit_index": index, "treatment": assignment[index],
                             "seed": run_seed, "error": str(exc)})
    if failures:
        # 运行失败可能与处理有关，不能丢弃后仅用成功运行估计效应。
        return {"status": "incomplete", "assignment": assignment,
                "records": records, "failures": failures, "effects": None}
    effects = {}
    for name in metric_names:
        control = [item["metrics"][name] for item in records if item["treatment"] == 0]
        treatment = [item["metrics"][name] for item in records if item["treatment"] == 1]
        effects[name] = independent_summary(control, treatment, alpha=alpha, seed=seed)
    return {"status": "complete", "assignment": assignment, "records": records,
            "failures": [], "effects": effects}
