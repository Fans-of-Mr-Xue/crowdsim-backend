"""SUMO post-analysis adapter for run-level causal discovery and validation.

The input contract matches postanalysis_api: one terminal document per
``(planId, seed)`` with ``features`` and versioned ``metrics``. Time steps are
not treated as independent samples. PC discovery and paired intervention
inference are delegated to the shared post-analysis service.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

import pandas as pd
from scipy.stats import pearsonr, spearmanr

from algorithms._inference import ordinary_least_squares
from algorithms.numeric import partial_correlation
from postanalysis_api.services.causal_analysis import analyze_completed_batch


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def build_experiment_dataframe(
    task: Mapping[str, Any], runs: Sequence[Mapping[str, Any]],
) -> tuple[pd.DataFrame, dict]:
    """Convert SUMO run documents to one wide row per plan/seed run.

    Columns include run provenance, ``feature_<featureId>`` values used by PC,
    and ``metric_<metricId>`` values. Features are already aggregated by the
    declared spatial region, time window, and aggregation rule; this function
    never expands a run into correlated frame-level rows.
    """
    if not isinstance(task, Mapping) or not isinstance(runs, Sequence):
        raise ValueError("task and runs must follow the post-analysis run contract")
    features = task.get("pcFeatures") or []
    metric_ids = task.get("metricIds") or []
    if not isinstance(features, list) or not isinstance(metric_ids, list):
        raise ValueError("task.pcFeatures and task.metricIds must be lists")

    groups = {group.get("id"): group for group in task.get("groups", [])
              if isinstance(group, Mapping)}
    records = []
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        plan_id = run.get("planId")
        group = groups.get(plan_id, {})
        row: dict[str, Any] = {
            "plan_id": plan_id,
            "seed": run.get("seed"),
            "status": run.get("status"),
            "is_baseline": plan_id == "F-00",
            "manipulated_feature_id": group.get("manipulatedFeatureId"),
        }
        run_features = run.get("features") or {}
        for feature in features:
            if isinstance(feature, Mapping) and isinstance(feature.get("id"), str):
                feature_id = feature["id"]
                row[f"feature_{feature_id}"] = _finite(run_features.get(feature_id))
        run_metrics = run.get("metrics") or {}
        for metric_id in metric_ids:
            envelope = run_metrics.get(metric_id) or {}
            row[f"metric_{metric_id}"] = _finite(envelope.get("value")) if isinstance(envelope, Mapping) else None
        records.append(row)

    if not records:
        raise ValueError("SUMO 实验结果中没有运行记录")
    dataframe = pd.DataFrame(records)
    metadata = {
        "sample_count": len(dataframe),
        "completed_run_count": int((dataframe["status"] == "completed").sum()),
        "independent_unit": "planId + seed; never a simulation frame",
        "baseline_plan_id": "F-00",
        "feature_columns": [column for column in dataframe if column.startswith("feature_")],
        "metric_columns": [column for column in dataframe if column.startswith("metric_")],
        "features": [dict(feature) for feature in features if isinstance(feature, Mapping)],
        "metric_ids": list(metric_ids),
    }
    return dataframe, metadata


def discover_correlations(
    dataframe: pd.DataFrame,
    method: str = "spearman",
    min_abs_corr: float = 0.2,
    max_p_value: float = 0.1,
    top_k: int = 30,
) -> dict:
    """Return exploratory baseline feature/metric associations, not causes."""
    if method not in {"pearson", "spearman"}:
        raise ValueError("method must be pearson or spearman")
    source = dataframe
    if "is_baseline" in dataframe:
        source = dataframe[dataframe["is_baseline"] == True]  # noqa: E712
    feature_columns = [column for column in source if column.startswith("feature_")]
    metric_columns = [column for column in source if column.startswith("metric_")]
    correlation = pearsonr if method == "pearson" else spearmanr
    pairs = []
    for feature in feature_columns:
        for metric in metric_columns:
            pair = source[[feature, metric]].dropna()
            if len(pair) < 4 or pair[feature].nunique() < 2 or pair[metric].nunique() < 2:
                continue
            coefficient, p_value = correlation(pair[feature], pair[metric])
            if abs(coefficient) >= min_abs_corr and p_value <= max_p_value:
                pairs.append({
                    "feature": feature.removeprefix("feature_"),
                    "metric": metric.removeprefix("metric_"),
                    "correlation": round(float(coefficient), 4),
                    "p_value": round(float(p_value), 6),
                    "sample_count": len(pair),
                })
    pairs.sort(key=lambda item: abs(item["correlation"]), reverse=True)
    return {
        "method": method,
        "associations": pairs[:top_k],
        "tested_pairs": len(feature_columns) * len(metric_columns),
        "note": "仅使用 F-00 独立种子运行作探索性关联筛选；相关性不构成因果候选边。因果候选由 PC 图给出。",
    }


def discover_causal_candidates(task: Mapping[str, Any], runs: Sequence[Mapping[str, Any]]) -> dict:
    """Discover candidate relations from independent F-00 SUMO runs with PC."""
    analysis = analyze_completed_batch(dict(task), [dict(run) for run in runs])
    return analysis["pc"]


def _path_report(analysis: dict, path: Sequence[str]) -> dict:
    if isinstance(path, (str, bytes)) or not isinstance(path, Sequence):
        raise ValueError("path must be an ordered sequence of feature IDs")
    nodes = list(path)
    if len(nodes) < 2 or len(nodes) > 8 or any(not isinstance(node, str) or not node for node in nodes):
        raise ValueError("path must contain 2 to 8 nonempty feature IDs")
    if len(set(nodes)) != len(nodes):
        raise ValueError("path cannot repeat a feature")

    pc = analysis.get("pc") or {}
    graph = pc.get("graph") or {}
    if pc.get("status") != "completed" or not graph:
        return {
            "status": "insufficient_pc_data",
            "path": nodes,
            "sample_count": pc.get("sampleCount", 0),
            "reason": pc.get("reason", "PC graph is unavailable"),
            "edges": [],
        }
    known = set(graph.get("variables", []))
    unknown = [node for node in nodes if node not in known]
    if unknown:
        return {"status": "unknown_feature", "path": nodes, "unknown_features": unknown, "edges": []}

    skeleton = {frozenset(edge) for edge in graph.get("skeleton", [])}
    supported = analysis.get("interventionSupportedDirections", [])
    edge_results = []
    for source, target in zip(nodes, nodes[1:]):
        is_candidate = frozenset((source, target)) in skeleton
        evidence = [item for item in supported
                    if item.get("source") == source and item.get("target") == target]
        edge_results.append({
            "source": source,
            "target": target,
            "pc_candidate": is_candidate,
            "intervention_evidence": evidence,
            "status": "intervention_supported" if evidence else
                      "candidate_unverified" if is_candidate else "not_a_pc_candidate",
        })

    if all(edge["status"] == "intervention_supported" for edge in edge_results):
        status = "intervention_supported"
    elif any(edge["status"] == "not_a_pc_candidate" for edge in edge_results):
        status = "not_a_pc_path"
    else:
        status = "candidate_path_unverified"
    return {
        "path": nodes,
        "status": status,
        "edges": edge_results,
        "note": "路径仅在每条相邻边都存在满足配对干预、时间顺序和隔离检查的证据时标记为支持；结论限定于该 SUMO 模型。",
    }


def validate_causal_path(
    task: Mapping[str, Any], runs: Sequence[Mapping[str, Any]], path: Sequence[str],
) -> dict:
    """Check each ordered edge in a proposed feature path against PC and runs."""
    analysis = analyze_completed_batch(dict(task), [dict(run) for run in runs])
    return _path_report(analysis, path)


def path_analysis(
    task: Mapping[str, Any], runs: Sequence[Mapping[str, Any]],
    path: Sequence[str] | None = None, dataset: Mapping[str, Any] | None = None,
) -> dict:
    """Return PC results or a clearly provisional regression SEM if PC fails."""
    analysis = analyze_completed_batch(dict(task), [dict(run) for run in runs])
    if analysis["pc"].get("status") != "completed":
        fallback = provisional_sem_from_experiment_runs(
            task, runs, reason=analysis["pc"].get("detail") or analysis["pc"].get("reason", "PC did not complete"),
        )
        if not fallback.get("edges") and dataset is not None:
            fallback = provisional_sem_from_uploaded_dataset(
                dataset, reason=analysis["pc"].get("detail") or analysis["pc"].get("reason", "PC did not complete"),
            )
        return {"pc": analysis["pc"], "provisional_sem": fallback,
                "path_validation": _provisional_path_report(fallback, path) if path else None}
    if path is None:
        return {
            "pc": analysis["pc"],
            "intervention_supported_directions": analysis["interventionSupportedDirections"],
            "note": "PC 的未定向边是候选关系；只有满足配对干预、时序与隔离检查的边才有方向支持。",
        }
    return _path_report(analysis, path)


_MEASURE_FIELDS = ("population", "density", "meanSpeed", "pressureProxy")
_PROVISIONAL_SEM_WARNING = (
    "这是基于观测关联的待验证 SEM 草案，不是 PC 因果图或已确认因果结论。"
    "回归筛选不能排除未测混杂、反向因果或选择偏差；上传时间线的相邻行存在时空相关，"
    "其样本数不能解释为独立重复。需用独立 SUMO 重复、明确干预和领域知识进一步验证。"
)


def build_uploaded_dataset_dataframe(dataset: Mapping[str, Any]) -> tuple[pd.DataFrame, dict]:
    """Create lag-aligned rows from uploaded timeline observations by region."""
    observations = dataset.get("observations") if isinstance(dataset, Mapping) else None
    if not isinstance(observations, list):
        raise ValueError("uploaded dataset must contain an observations list")
    by_region: dict[str, list[Mapping[str, Any]]] = {}
    for row in observations:
        if isinstance(row, Mapping) and isinstance(row.get("regionId"), str):
            by_region.setdefault(row["regionId"], []).append(row)
    records = []
    for region_id, rows in by_region.items():
        ordered = sorted(rows, key=lambda item: item.get("timeSeconds", -1))
        for index in range(len(ordered) - 2):
            window = ordered[index:index + 3]
            times = [item.get("timeSeconds") for item in window]
            if any(not isinstance(value, int) or isinstance(value, bool) for value in times):
                continue
            if not times[0] < times[1] < times[2]:
                continue
            record: dict[str, Any] = {
                "region_id": region_id,
                "time_t0": times[0], "time_t1": times[1], "time_t2": times[2],
            }
            for lag, observation in enumerate(window):
                for field in _MEASURE_FIELDS:
                    record[f"{field}_t{lag}"] = _finite(observation.get(field))
            records.append(record)
    if not records:
        return pd.DataFrame(), {"sample_count": 0, "regions": len(by_region)}
    frame = pd.DataFrame(records)
    return frame, {
        "sample_count": len(frame),
        "regions": len(by_region),
        "fields": list(_MEASURE_FIELDS),
        "lag_unit": "ordered observation steps; time gaps may vary",
        "source_kind": dataset.get("sourceKind"),
    }


def _regression_link(
    rows: Sequence[Mapping[str, Any]], source: str, target: str,
    controls: Sequence[str], min_abs_association: float,
) -> dict | None:
    predictors = [source, *[key for key in controls if key not in {source, target}]]
    complete = [row for row in rows
                if all(_finite(row.get(key)) is not None for key in [target, *predictors])]
    if len(complete) < max(4, len(predictors) + 3):
        return None
    try:
        x_values = [[float(row[key]) for key in predictors] for row in complete]
        outcome = [float(row[target]) for row in complete]
        design = [[1.0, *values] for values in x_values]
        coefficients = ordinary_least_squares(design, outcome)
        association = partial_correlation(
            [row[source] for row in complete],
            [row[target] for row in complete],
            [[row[key] for row in complete] for key in predictors if key != source],
        )
    except (ValueError, ArithmeticError):
        return None
    if not math.isfinite(association) or abs(association) < min_abs_association:
        return None
    return {
        "association": round(float(association), 4),
        "slope": round(float(coefficients[predictors.index(source) + 1]), 6),
        "sample_count": len(complete),
    }


def _provisional_sem(
    rows: Sequence[Mapping[str, Any]], variables: Sequence[Mapping[str, Any]], *,
    source: str, reason: str, min_abs_association: float = 0.2,
) -> dict:
    usable = [item for item in variables if item.get("key")]
    edges = []
    for left in usable:
        for right in usable:
            if left["end"] > right["start"]:
                continue
            same_stage_controls = [item["key"] for item in usable
                                   if item["start"] == left["start"] and item["key"] != left["key"]]
            fit = _regression_link(rows, left["key"], right["key"],
                                   same_stage_controls, min_abs_association)
            if fit:
                edges.append({"source": left["id"], "target": right["id"], **fit,
                              "status": "provisional_association"})

    mediated_paths = []
    for source_node in usable:
        for mediator in usable:
            if source_node["end"] > mediator["start"]:
                continue
            stage1_controls = [item["key"] for item in usable
                               if item["start"] == source_node["start"] and item["key"] != source_node["key"]]
            first = _regression_link(rows, source_node["key"], mediator["key"],
                                     stage1_controls, min_abs_association)
            if not first:
                continue
            for outcome in usable:
                if mediator["end"] > outcome["start"] or outcome["key"] in {source_node["key"], mediator["key"]}:
                    continue
                stage2_controls = [source_node["key"], *[
                    item["key"] for item in usable
                    if item["start"] <= mediator["start"] and item["key"] not in {source_node["key"], mediator["key"]}
                ]]
                second = _regression_link(rows, mediator["key"], outcome["key"],
                                          stage2_controls, min_abs_association)
                if second:
                    mediated_paths.append({
                        "nodes": [source_node["id"], mediator["id"], outcome["id"]],
                        "stage1": first,
                        "stage2": second,
                        "association_product": round(first["association"] * second["association"], 6),
                        "status": "provisional_two_stage_regression",
                    })
    mediated_paths.sort(key=lambda item: abs(item["association_product"]), reverse=True)
    return {
        "status": "provisional_sem" if edges or mediated_paths else "insufficient_data",
        "model_type": "time_ordered_observational_path_screen",
        "source": source,
        "pc_failure_reason": reason,
        "sample_count": len(rows),
        "minimum_abs_partial_association": min_abs_association,
        "nodes": [{"id": item["id"], "label": item.get("label", item["id"]),
                   "time_start": item["start"], "time_end": item["end"]} for item in usable],
        "edges": sorted(edges, key=lambda item: abs(item["association"]), reverse=True)[:100],
        "two_stage_paths": mediated_paths[:100],
        "validated": False,
        "warning": _PROVISIONAL_SEM_WARNING,
    }


def provisional_sem_from_experiment_runs(
    task: Mapping[str, Any], runs: Sequence[Mapping[str, Any]], *, reason: str,
) -> dict:
    """Regression fallback on completed F-00 seed-runs when PC is unavailable."""
    dataframe, _ = build_experiment_dataframe(task, runs)
    baseline = dataframe[(dataframe["is_baseline"] == True) & (dataframe["status"] == "completed")]  # noqa: E712
    features = task.get("pcFeatures") or []
    variables = [{
        "id": feature["id"], "key": f"feature_{feature['id']}",
        "label": feature["id"], "start": feature["windowStartSeconds"],
        "end": feature["windowEndSeconds"],
    } for feature in features if isinstance(feature, Mapping) and
         isinstance(feature.get("windowStartSeconds"), int) and isinstance(feature.get("windowEndSeconds"), int)]
    rows = baseline.to_dict("records")
    return _provisional_sem(rows, variables, source="completed_F-00_seed_runs", reason=reason)


def provisional_sem_from_uploaded_dataset(
    dataset: Mapping[str, Any], *, reason: str = "uploaded observations are not independent PC run samples",
) -> dict:
    """Build an explicitly observational lagged path screen from uploaded data."""
    frame, metadata = build_uploaded_dataset_dataframe(dataset)
    variables = [{"id": f"{field}@t{lag}", "key": f"{field}_t{lag}",
                  "label": f"{field} at step {lag}", "start": lag, "end": lag}
                 for field in _MEASURE_FIELDS for lag in range(3)]
    result = _provisional_sem(frame.to_dict("records"), variables,
                              source="uploaded_dataset_timeline", reason=reason)
    result["sample_unit_note"] = (
        "每行是同一区域三个连续观测点构成的滑动时间窗；相邻行有重叠，不能按独立样本解释。"
    )
    result["data_metadata"] = metadata
    return result


def _provisional_path_report(sem: Mapping[str, Any], path: Sequence[str]) -> dict:
    if isinstance(path, (str, bytes)) or not isinstance(path, Sequence):
        raise ValueError("path must be an ordered sequence of feature IDs")
    nodes = list(path)
    if len(nodes) < 2 or len(nodes) > 8 or any(not isinstance(node, str) or not node for node in nodes):
        raise ValueError("path must contain 2 to 8 nonempty feature IDs")
    if len(set(nodes)) != len(nodes):
        raise ValueError("path cannot repeat a feature")
    edges = sem.get("edges", [])
    proposed = {(edge.get("source"), edge.get("target")): edge for edge in edges}
    checked = [{"source": source, "target": target,
                "provisional_edge": proposed.get((source, target))}
               for source, target in zip(nodes, nodes[1:])]
    present = all(edge["provisional_edge"] is not None for edge in checked)
    return {"status": "provisional_only" if present else "path_not_screened",
            "path": nodes, "edges": checked, "validated": False,
            "warning": sem.get("warning", _PROVISIONAL_SEM_WARNING)}


def causal_summary_from_dataset(dataset: Mapping[str, Any]) -> dict:
    """Return an observational provisional SEM for an uploaded timeline."""
    sem = provisional_sem_from_uploaded_dataset(dataset)
    return {"causal_candidates": None, "provisional_sem": sem,
            "warning": _PROVISIONAL_SEM_WARNING}


def causal_summary(
    task: Mapping[str, Any], runs: Sequence[Mapping[str, Any]],
    path: Sequence[str] | None = None, dataset: Mapping[str, Any] | None = None,
) -> dict:
    """Build a SUMO wide table and reuse the shared PC/intervention pipeline."""
    dataframe, metadata = build_experiment_dataframe(task, runs)
    analysis = analyze_completed_batch(dict(task), [dict(run) for run in runs])
    result = {
        "meta": metadata,
        "exploratory_associations": discover_correlations(dataframe),
        "causal_candidates": analysis["pc"],
        "intervention_analysis": analysis,
    }
    if analysis["pc"].get("status") != "completed":
        provisional = provisional_sem_from_experiment_runs(
            task, runs, reason=analysis["pc"].get("detail") or analysis["pc"].get("reason", "PC did not complete"),
        )
        if not provisional.get("edges") and dataset is not None:
            provisional = provisional_sem_from_uploaded_dataset(
                dataset, reason=analysis["pc"].get("detail") or analysis["pc"].get("reason", "PC did not complete"),
            )
        result["provisional_sem"] = provisional
    if path is not None:
        if analysis["pc"].get("status") == "completed":
            result["path_validation"] = _path_report(analysis, path)
        else:
            result["path_validation"] = _provisional_path_report(result["provisional_sem"], path)
    return result
