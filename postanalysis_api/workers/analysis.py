"""Paired seed inference and explicit mechanism ablation evidence, no preview values."""
from __future__ import annotations

import math
import random
from statistics import mean, stdev

from postanalysis_api.schemas.metrics import METRICS, METRIC_VERSION
from .causal import discover


def paired_effect(runs, reference, candidate, metric):
    def values(group):
        return {run["seed"]: run["metrics"][metric] for run in runs
                if run["planId"] == group and run.get("status") == "completed"
                and isinstance(run.get("metrics", {}).get(metric), (int, float))}
    left, right = values(reference), values(candidate)
    seeds = sorted(left.keys() & right.keys())
    delta = [right[seed] - left[seed] for seed in seeds]
    result = {"referenceGroupId": reference, "groupId": candidate, "metricId": metric,
              "unit": METRICS[metric]["unit"], "pairCount": len(seeds), "seeds": seeds,
              "estimate": mean(delta) if delta else None, "ci95": None,
              "direction": "candidate_minus_reference", "method": "paired_seed_bootstrap"}
    if len(seeds) >= 2:
        rng = random.Random(20261023)
        boot = sorted(mean(rng.choices(delta, k=len(delta))) for _ in range(2000))
        result["ci95"] = [boot[49], boot[1949]]
    result["inferenceStatus"] = "descriptive_only" if len(seeds) < 20 else "paired_seed_estimate"
    return result


def summarize(task, runs):
    complete = [run for run in runs if run.get("status") == "completed"]
    metric_ids = task["metricIds"]
    ids = ["F-00", *[group["id"] for group in task.get("groups", [])]]
    distributions = []
    for group in ids:
        for metric in metric_ids:
            samples = [run["metrics"].get(metric) for run in complete if run["planId"] == group]
            samples = [value for value in samples if isinstance(value, (int, float)) and math.isfinite(value)]
            distributions.append({"groupId": group, "metricId": metric, "n": len(samples),
                                  "mean": mean(samples) if samples else None,
                                  "std": stdev(samples) if len(samples) > 1 else None,
                                  "unit": METRICS[metric]["unit"]})
    comparison, sections = [], {key: [] for key in ("observation", "intervention", "mechanism")}
    for group in task.get("groups", []):
        reference = group.get("referenceGroupId", "F-00")
        effects = [paired_effect(complete, reference, group["id"], metric) for metric in metric_ids]
        comparison.extend(effects)
        sections[group.get("experimentType", "observation")].append(
            {"groupId": group["id"], "name": group["name"], "hypothesis": group["hypothesis"],
             "referenceGroupId": reference, "interventions": group["interventions"], "effects": effects})
    causal_graph = discover(task.get("pcFeatures", []), complete) if sections["intervention"] else {"status": "not_designed", "reason": "未设计干预分析"}
    return {"source": "sumo", "definitionVersion": "post-analysis/1.0", "metricDefinitionVersion": METRIC_VERSION,
            "metrics": {"distributions": distributions, "runs": [{key: run.get(key) for key in
                ("id", "planId", "seed", "status", "metrics", "archive", "measurement", "executionEvidence")} | {
                    "calibration": {"populationMAE": run.get("calibration", {}).get("populationMAE"),
                                    "comparisonCount": len(run.get("calibration", {}).get("populationResiduals", []))}} for run in runs]},
            "comparison": {"effects": comparison, "method": "paired seeds; 2000 bootstrap resamples; candidate minus reference"},
            **{key: {"experiments": value, "status": "computed" if value else "not_designed", **({"causalGraph": causal_graph} if key == "intervention" else {})} for key, value in sections.items()},
            "causalGraph": causal_graph,
            "limitations": ["结论针对此 SUMO 测试模型；不代表历史事故因果事实",
                            "当前入口映射与驻留/入流重建未做拟合校准，校准误差和极端拥挤输出均须保留审核",
                            "重复种子少于20时仅提供描述性估计；缺失和右删失指标不填零",
                            "机制结果是预定义模块消融的配对效应，不自动解释为自然间接效应"]}
