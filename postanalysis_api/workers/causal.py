"""Post-only adapter for the shared Gaussian PC-Stable implementation.

Only independent F-00 seeds enter discovery. Paired worlds and time samples
from one run never inflate the independent sample count.
"""
from math import isfinite

from algorithms.pc import pc_discovery


def discover(features, runs):
    nodes = [{key: feature[key] for key in ("id", "metricId", "regionId", "windowStartSeconds", "windowEndSeconds")}
             for feature in features]
    ids = [node["id"] for node in nodes]
    baseline = [run for run in runs if run["planId"] == "F-00" and run.get("status") == "completed"]
    seeds = [run.get("seed") for run in baseline]
    if len(set(seeds)) != len(seeds) or any(type(seed) is not int for seed in seeds):
        return {"status": "invalid_data", "nodes": nodes, "edges": [], "sampleCount": 0,
                "reason": "独立基线种子缺失或重复，不能将重复运行当作独立样本。"}
    rows = [{key: run["featureValues"][key] for key in ids} for run in baseline
            if all(type(run.get("featureValues", {}).get(key)) in (int, float)
                   and isfinite(run["featureValues"][key]) for key in ids)]
    result = {"nodes": nodes, "edges": [], "sampleCount": len(rows), "sampleUnit": "independent baseline seed",
              "algorithm": "Gaussian PC-Stable / Fisher-Z / Meek R1-R3", "alpha": 0.05,
              "requiredMinimum": max(20, len(nodes) + 4),
              "assumptions": ["独立样本、线性高斯条件独立检验、无环性、因果充分性与忠实性。",
                              "未识别方向保留无向；奇异检验和定向冲突显式保留。候选图不代表历史真实因果。"]}
    if not 2 <= len(nodes) <= 8 or len(rows) < result["requiredMinimum"]:
        return {**result, "status": "insufficient_data",
                "reason": "PC-Stable 使用独立 F-00 种子，至少20个完整样本，支持2—8个特征；配对世界线不当作独立样本。"}
    try:
        graph = pc_discovery(rows, alpha=0.05, stable=True)
    except ValueError as exc:
        return {**result, "status": "degenerate_data", "reason": f"特征方差或条件独立检验不满足要求：{exc}"}
    edges = [{"source": source, "target": target, "orientation": "directed"} for source, target in graph["directed_edges"]]
    edges += [{"source": source, "target": target, "orientation": "undirected"} for source, target in graph["undirected_edges"]]
    uncertain = graph["graph_type"] != "CPDAG"
    return {**result, "status": "partially_estimable" if uncertain else "computed", "edges": edges,
            "graphType": graph["graph_type"], "tests": graph["test_log"],
            "separatingSets": graph["separating_sets"], "orientationConflicts": graph["conflicts"],
            "singularTestCount": graph["invalid_test_count"],
            "reason": "存在无效条件独立检验或定向冲突，结果按不确定候选图保留。" if uncertain else None}
