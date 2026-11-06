"""Paired seed inference and explicit mechanism ablation evidence, no preview values."""
from __future__ import annotations

import math
import random
import logging
from statistics import mean, stdev

from postanalysis_api.schemas.metrics import METRICS, METRIC_VERSION
from .causal import discover


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def paired_effect(runs, reference, candidate, metric):
    def values(group):
        result, seen = {}, set()
        for run in runs:
            if run["planId"] != group or run.get("status") != "completed":
                continue
            seed = run.get("seed")
            if type(seed) is not int or seed in seen:
                raise ValueError("配对分析需要每组唯一且有效的随机种子")
            seen.add(seed)
            value = run.get("metrics", {}).get(metric)
            if finite(value):
                result[seed] = value
        return result
    left, right = values(reference), values(candidate)
    seeds = sorted(left.keys() & right.keys())
    delta = [right[seed] - left[seed] for seed in seeds]
    result = {"referenceGroupId": reference, "groupId": candidate, "metricId": metric,
              "unit": METRICS[metric]["unit"], "pairCount": len(seeds), "seeds": seeds,
              "estimate": mean(delta) if delta else None, "ci95": None,
              "maxAbsolutePairDifference": max(map(abs, delta)) if delta else None,
              "direction": "candidate_minus_reference", "method": "paired_seed_bootstrap"}
    if len(seeds) >= 2:
        rng = random.Random(20261023)
        boot = sorted(mean(rng.choices(delta, k=len(delta))) for _ in range(2000))
        result["ci95"] = [boot[49], boot[1949]]
    result["inferenceStatus"] = "descriptive_only" if len(seeds) < 20 else "paired_seed_estimate"
    result["referenceValidCount"], result["candidateValidCount"] = len(left), len(right)
    result["excludedUnpairedCount"] = len(left.keys() ^ right.keys())
    return result


def assess_hypothesis(design, effects):
    """Pre-registered primary endpoint only; secondary metrics do not vote."""
    primary = design.get("primaryMetricId")
    effect = next((item for item in effects if item["metricId"] == primary), None)
    direction = design.get("expectedDirection")
    result = {"primaryMetricId": primary, "expectedDirection": direction,
              "rule": "primary_endpoint_paired_ci95", "scope": "SUMO model intervention effect",
              "multipleTesting": "unadjusted_per_contrast; no family-wise claim"}
    if not effect or effect["estimate"] is None:
        result.update(status="missing_data", reason="主要终点无完整配对，不能判定假设。")
    elif effect["pairCount"] < 20:
        result.update(status="descriptive_only", reason="少于20个独立配对，仅报告效应与区间，不判定假设成立。")
    elif not effect["ci95"]:
        result.update(status="inconclusive", reason="主要终点区间不可估计。")
    else:
        lower, upper = effect["ci95"]
        separated = upper < 0 or lower > 0
        supported = separated and (direction == "two_sided" or direction == "decrease" and upper < 0 or direction == "increase" and lower > 0)
        result.update(status="supported" if supported else "opposed" if separated else "inconclusive",
                      reason="按预登记主要终点及方向解释逐对照95%区间；区间包含零时证据不足。")
    return result


def summarize(task, runs):
    complete = [run for run in runs if run.get("status") == "completed"]
    metric_ids = task["metricIds"]
    ids = ["F-00", *[group["id"] for group in task.get("groups", [])]]
    distributions = []
    for group in ids:
        for metric in metric_ids:
            samples = [run["metrics"].get(metric) for run in complete if run["planId"] == group]
            samples = [value for value in samples if finite(value)]
            distributions.append({"groupId": group, "metricId": metric, "n": len(samples),
                                  "missingCount": sum(run["planId"] == group for run in complete) - len(samples),
                                  "mean": mean(samples) if samples else None,
                                  "std": stdev(samples) if len(samples) > 1 else None,
                                  "unit": METRICS[metric]["unit"]})
    comparison, sections = [], {key: [] for key in ("observation", "intervention", "mechanism")}
    designs = task.get("reviewSnapshot", {}).get("counterfactualDesign", {}).get("groups", [])
    unified = any(group.get("experimentType") == "counterfactual" for group in task.get("groups", []))
    for group in task.get("groups", []):
        reference = group.get("referenceGroupId", "F-00")
        effects = [paired_effect(complete, reference, group["id"], metric) for metric in metric_ids]
        comparison.extend(effects)
        row = {"groupId": group["id"], "name": group["name"], "hypothesis": group["hypothesis"],
               "sourceGroupId": group.get("sourceGroupId"), "referenceGroupId": reference, "interventions": group["interventions"], "effects": effects}
        if group.get("experimentType") == "counterfactual":
            from .memorial_scenario import control_schedule
            other = next((item for item in task["groups"] if item["id"] == reference), None)
            factual_controls = next((run.get('controls') for run in complete if run['planId'] == 'F-00' and run.get('controls')), None)
            left, right = control_schedule(other["interventions"] if other else [], factual_controls), control_schedule(group["interventions"], factual_controls)
            changed = [key for key in left if left[key] != right[key]]
            timing_only = set(changed) == {"gateOpenSeconds", "guidanceStartSeconds"} and left["gateOpenSeconds"] - right["gateOpenSeconds"] == left["guidanceStartSeconds"] - right["guidanceStartSeconds"]
            sections["observation"].append(row)
            if len(changed) <= 1 or timing_only:
                sections["intervention"].append(row)
            design = next((item for item in designs if item["id"] == group.get("sourceGroupId")), {})
            row.update({key: design.get(key) for key in ("primaryMetricId", "expectedDirection", "decisionRule", "factorSymbols", "method")})
            row["hypothesisAssessment"] = assess_hypothesis(design, effects)
            row["changedControls"] = changed
            if not changed:
                usable = [item for item in effects if item["pairCount"]]
                row["placeboCheck"] = {"status": "insufficient_data" if not usable else "passed" if all(item["maxAbsolutePairDifference"] == 0 for item in usable) else "failed",
                                       "checkedMetricCount": len(usable),
                                       "reason": "同控制、同流量、同种子组的可测指标应相同；缺失终点不视为零。"}
            if design.get("method") == "ablation" and other and len(changed) == 1 and changed[0] in {"compliance", "guidanceDelaySeconds"}:
                sections["mechanism"].append(row)
        else:
            sections[group.get("experimentType", "observation")].append(row)
    causal_graph = discover(task.get("pcFeatures", []), complete) if sections["intervention"] or unified else {"status": "not_designed", "reason": "未设计干预分析"}
    result = {"source": "sumo", "definitionVersion": "post-analysis/2.0" if unified else "post-analysis/1.0", "metricDefinitionVersion": METRIC_VERSION,
            "metricCatalog": {metric: METRICS[metric] for metric in metric_ids},
            "causalProtocol": {"estimand": "E[Y(do(candidate), U) - Y(do(reference), U)]",
                               "unit": "independent simulation seed; paired across worlds",
                               "identification": "controlled simulator intervention; fixed demand, network, model and common random numbers",
                               "estimation": "paired mean difference; 2000 paired bootstrap resamples",
                               "refutation": "identical-control contrast; OFAT isolation; guidance module ablation",
                               "primaryEndpointPolicy": "pre-registered per contrast; secondary outcomes descriptive; intervals unadjusted across contrasts"},
            "metrics": {"catalog": {metric: METRICS[metric] for metric in metric_ids},
                        "distributions": distributions, "runs": [{key: run.get(key) for key in
                ("id", "planId", "seed", "status", "metrics", "archive", "measurement", "executionEvidence")} | {
                    "calibration": {"populationMAE": run.get("calibration", {}).get("populationMAE"),
                                    "comparisonCount": len(run.get("calibration", {}).get("populationResiduals", []))}} for run in runs]},
            "comparison": {"effects": comparison, "method": "paired seeds; 2000 bootstrap resamples; candidate minus reference"},
            **{key: {"experiments": value, "status": "computed" if value else "no_eligible_contrast" if unified else "not_designed", **({"causalGraph": causal_graph} if key == "intervention" else {})} for key, value in sections.items()},
            "causalGraph": causal_graph,
            "limitations": ["结论针对此 SUMO 测试模型；不代表历史事故因果事实",
                            "当前入口映射与驻留/入流重建未做拟合校准，校准误差和极端拥挤输出均须保留审核",
                            "重复种子少于20时仅提供描述性估计；缺失和右删失指标不填零",
                            "机制结果是预定义模块消融的配对效应，不自动解释为自然间接效应"]}
    # Method boundaries stay in backend diagnostics, separate from user readings.
    notes = [*result["limitations"], *causal_graph.get("assumptions", [])]
    if causal_graph.get("reason"):
        notes.append(causal_graph["reason"])
    for rows in sections.values():
        for row in rows:
            for field in ("hypothesisAssessment", "placeboCheck"):
                if row.get(field, {}).get("reason"):
                    notes.append(row[field]["reason"])
    for run in runs:
        notes.extend(run.get("assumptions", []))
    result["diagnostics"] = list(dict.fromkeys(notes))
    for note in result["diagnostics"]:
        logging.getLogger("postanalysis_api.analysis").info("Post analysis: %s", note)
    return result
