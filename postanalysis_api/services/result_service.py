"""Seal auditable group summaries and three analysis-stage payloads."""

from __future__ import annotations

from statistics import mean

from algorithms._inference import paired_summary
from postanalysis_api.schemas.common import ApiError, metric_value
from postanalysis_api.schemas.metrics import METRICS, METRIC_VERSION
from .causal_analysis import analyze_completed_batch, _finite


def _run_metric(run: dict, metric_id: str) -> float | None:
    envelope = (run.get("metrics") or {}).get(metric_id) or {}
    if envelope.get("definitionVersion") != METRIC_VERSION or envelope.get("unit") != METRICS[metric_id]["unit"]:
        return None
    return _finite(envelope.get("value"))


def build_result(task: dict, runs: list[dict]) -> dict:
    """Only completed real runs can contribute; missing evidence remains null."""
    analysis = analyze_completed_batch(task, runs)
    by_key = {(run["planId"], run["seed"]): run for run in runs}
    baseline_seeds = [task["seedStart"] + offset for offset in range(max(group["repeatCount"] for group in task["groups"]))]
    plan_specs = [("F-00", baseline_seeds)] + [
        (group["id"], baseline_seeds[:group["repeatCount"]]) for group in task["groups"]]
    aggregates = {}
    for plan_id, seeds in plan_specs:
        values = {}
        for metric_id in task["metricIds"]:
            observations = [_run_metric(by_key[(plan_id, seed)], metric_id)
                            if by_key[(plan_id, seed)]["status"] == "completed" else None for seed in seeds]
            missing = None if all(value is not None for value in observations) else "MISSING_OR_FAILED_RUN"
            values[metric_id] = metric_value(mean(observations) if missing is None else None,
                                             METRICS[metric_id]["unit"], METRIC_VERSION,
                                             missing_reason=missing)
            values[metric_id]["sampleCount"] = len([value for value in observations if value is not None])
            values[metric_id]["plannedCount"] = len(seeds)
        aggregates[plan_id] = values

    comparison = []
    for group in task["groups"]:
        seeds = baseline_seeds[:group["repeatCount"]]
        effects = {}
        for metric_id in task["metricIds"]:
            aligned = [(_run_metric(by_key[("F-00", seed)], metric_id),
                        _run_metric(by_key[(group["id"], seed)], metric_id)) for seed in seeds]
            complete = all(by_key[("F-00", seed)]["status"] == "completed" and
                           by_key[(group["id"], seed)]["status"] == "completed" for seed in seeds)
            valid = [(left, right) for left, right in aligned if left is not None and right is not None]
            if not complete or len(valid) != len(seeds) or len(valid) < 2:
                effects[metric_id] = {"effect": None, "ci95": None, "unit": METRICS[metric_id]["unit"],
                                      "definitionVersion": "paired-effect/1.0", "validPairs": len(valid),
                                      "plannedPairs": len(seeds), "missingReason": "MISSING_OR_FAILED_PAIR"}
            else:
                summary = paired_summary([pair[0] for pair in valid], [pair[1] for pair in valid],
                                         seed=task["seedStart"])
                effects[metric_id] = {"effect": summary["effect"], "ci95": list(summary["ci"]),
                                      "unit": METRICS[metric_id]["unit"], "definitionVersion": "paired-effect/1.0",
                                      "validPairs": len(valid), "plannedPairs": len(seeds), "missingReason": None}
        comparison.append({"groupId": group["id"], "baselinePlanId": "F-00", "effects": effects})

    causal_graph = {"pc": analysis["pc"],
                    "interventionSupportedDirections": analysis["interventionSupportedDirections"]}
    result = {"datasetVersion": task["datasetVersion"], "definitionVersion": METRIC_VERSION,
              "metrics": aggregates, "comparison": comparison, "causalGraph": causal_graph,
              "observation": {"candidateGraph": analysis["pc"], "baselineRunCount": len(baseline_seeds)},
              "intervention": {"groups": analysis["groups"],
                               "supportedDirections": analysis["interventionSupportedDirections"]},
              "analysisStatus": analysis["analysisStatus"]}
    # Mechanism analysis needs actual switch/decision/message event evidence.
    # It is deliberately absent until the SUMO adapter supplies that evidence.
    return result
