"""Run-level F-00 PC discovery followed by paired intervention evidence."""

from __future__ import annotations

import math

from algorithm._inference import paired_summary
from algorithm.pc import pc_discovery
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.schemas.metrics import METRICS


def _finite(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def analyze_completed_batch(task: dict, runs: list[dict]) -> dict:
    """Analyze real, terminal runs; one row is one complete seed-run.

    Run schema: planId, seed, status, features{featureId:number},
    metrics{metricId:{value,unit,definitionVersion,missingReason}},
    isolationVerified. An adapter must persist provenance before calling this.
    """
    groups = task["groups"]
    max_repeat = max(group["repeatCount"] for group in groups)
    baseline_seeds = [task["seedStart"] + offset for offset in range(max_repeat)]
    by_key: dict[tuple[str, int], dict] = {}
    for run in runs:
        key = (run.get("planId"), run.get("seed"))
        if key in by_key:
            raise ApiError("DUPLICATE_RUN", "同一实验组和种子出现重复运行", 409)
        by_key[key] = run
    expected = {("F-00", seed) for seed in baseline_seeds}
    expected.update((group["id"], task["seedStart"] + index)
                    for group in groups for index in range(group["repeatCount"]))
    if set(by_key) != expected:
        raise ApiError("RUN_SET_MISMATCH", "运行集合与已锁定实验方案不一致", 409)
    if any(run.get("status") not in {"completed", "failed", "cancelled"} for run in runs):
        raise ApiError("TASK_NOT_TERMINAL", "所有计划运行结束后才能分析", 409)

    feature_ids = [feature["id"] for feature in task["pcFeatures"]]
    baseline_rows = []
    for seed in baseline_seeds:
        run = by_key[("F-00", seed)]
        if run["status"] != "completed":
            continue
        values = {key: _finite((run.get("features") or {}).get(key)) for key in feature_ids}
        if all(value is not None for value in values.values()):
            baseline_rows.append(values)
    minimum = max(5, len(feature_ids) + 4)
    if len(baseline_rows) < minimum:
        pc_result = {"status": "insufficient_data", "sampleCount": len(baseline_rows),
                     "requiredMinimum": minimum, "reason": "F00_COMPLETE_ROWS_INSUFFICIENT"}
    else:
        try:
            graph = pc_discovery(baseline_rows)
            pc_result = {"status": "completed", "sampleCount": len(baseline_rows),
                         "algorithm": "Gaussian-PC-stable", "graph": graph}
        except ValueError as exc:
            pc_result = {"status": "insufficient_data", "sampleCount": len(baseline_rows),
                         "reason": "PC_ASSUMPTIONS_OR_VARIANCE_FAILED", "detail": str(exc)}

    by_feature = {feature["id"]: feature for feature in task["pcFeatures"]}
    undirected = {frozenset(edge) for edge in pc_result.get("graph", {}).get("undirected_edges", [])}
    group_results = []
    supported_directions = []
    for group in groups:
        seeds = baseline_seeds[:group["repeatCount"]]
        paired_runs = [(by_key[("F-00", seed)], by_key[(group["id"], seed)]) for seed in seeds]
        effects = {}
        for feature_id in feature_ids:
            valid = []
            for baseline, candidate in paired_runs:
                left = _finite((baseline.get("features") or {}).get(feature_id)) if baseline["status"] == "completed" else None
                right = _finite((candidate.get("features") or {}).get(feature_id)) if candidate["status"] == "completed" else None
                if left is not None and right is not None:
                    valid.append((left, right))
            unit = METRICS[by_feature[feature_id]["metricId"]]["unit"]
            if len(valid) != len(paired_runs) or len(valid) < 2:
                effects[feature_id] = {"status": "incomplete", "validPairs": len(valid),
                                       "plannedPairs": len(paired_runs), "unit": unit,
                                       "definitionVersion": "paired-effect/1.0",
                                       "missingReason": "MISSING_OR_FAILED_PAIR"}
            else:
                summary = paired_summary([item[0] for item in valid], [item[1] for item in valid],
                                         seed=task["seedStart"])
                effects[feature_id] = {"status": "completed", "validPairs": len(valid),
                                       "effect": summary["effect"], "ci95": list(summary["ci"]),
                                       "ciMethod": summary["ci_method"], "unit": unit,
                                       "definitionVersion": "paired-effect/1.0"}
        source = group["manipulatedFeatureId"]
        for target in feature_ids:
            if target == source or frozenset((source, target)) not in undirected:
                continue
            effect = effects[target]
            temporal = by_feature[source]["windowEndSeconds"] <= by_feature[target]["windowStartSeconds"]
            isolated = all(candidate.get("isolationVerified") is True for _, candidate in paired_runs)
            ci = effect.get("ci95")
            if temporal and isolated and ci and (ci[0] > 0 or ci[1] < 0):
                supported_directions.append({"source": source, "target": target, "groupId": group["id"],
                                             "effect": effect["effect"], "ci95": ci,
                                             "status": "intervention_supported"})
        group_results.append({"groupId": group["id"], "plannedPairs": len(paired_runs),
                              "completedPairs": sum(left["status"] == right["status"] == "completed"
                                                    for left, right in paired_runs), "effects": effects})
    all_effects_complete = all(effect["status"] == "completed" for group in group_results for effect in group["effects"].values())
    return {"analysisStatus": "completed" if pc_result["status"] == "completed" and all_effects_complete else "partial",
            "pc": pc_result, "groups": group_results,
            "interventionSupportedDirections": supported_directions,
            "note": "干预证据支持仿真模型内的方向；未满足隔离与时间条件的边保留未定向"}
