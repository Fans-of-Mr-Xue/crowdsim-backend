"""Paired-seed comparisons for experiment reports."""

from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Any


def paired_comparison(rows: list[dict[str, Any]], baseline: str, candidate: str, metric: str, *, lower_is_better: bool) -> dict[str, Any]:
    baseline_by_seed = {row["seed"]: row.get(metric) for row in rows if row.get("methodId") == baseline and isinstance(row.get(metric), (int, float))}
    candidate_by_seed = {row["seed"]: row.get(metric) for row in rows if row.get("methodId") == candidate and isinstance(row.get(metric), (int, float))}
    seeds = sorted(set(baseline_by_seed) & set(candidate_by_seed))
    if not seeds:
        return {"baseline": baseline, "candidate": candidate, "metric": metric, "pairCount": 0}
    differences = []
    for seed in seeds:
        raw = float(baseline_by_seed[seed]) - float(candidate_by_seed[seed])
        differences.append(raw if lower_is_better else -raw)
    average = mean(differences)
    spread = stdev(differences) if len(differences) > 1 else 0.0
    standard_error = spread / math.sqrt(len(differences)) if differences else 0.0
    critical = 1.96
    p_value = None
    test = "descriptive_only"
    if len(differences) > 1 and spread > 1e-12:
        try:
            from scipy import stats

            result = stats.ttest_rel(
                [baseline_by_seed[seed] for seed in seeds],
                [candidate_by_seed[seed] for seed in seeds],
            )
            p_value = float(result.pvalue)
            critical = float(stats.t.ppf(0.975, len(differences) - 1))
            test = "paired_t"
        except Exception:
            pass
    return {
        "baseline": baseline,
        "candidate": candidate,
        "metric": metric,
        "pairCount": len(seeds),
        "seeds": seeds,
        "meanBenefit": average,
        "confidence95": [average - critical * standard_error, average + critical * standard_error],
        "effectSizeDz": average / spread if spread > 0 else None,
        "test": test,
        "pValue": p_value,
        "benefitDirection": "positive_is_better",
    }
