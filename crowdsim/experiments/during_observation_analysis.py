"""Read-only, reproducible evidence for class-one experiment interpretation.

The model explains computed evidence; it never estimates statistics from a chart.
STRESS reporting and AnyLogic's separation of spatial/temporal statistics inform
the contract in skills/during_experiment_analysis/SKILL.md.
"""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from statistics import mean

from skills.skill_loader import load_skill
from .during_observation import METRICS, number, values_at


ANALYSIS_VERSION = "during-observation-analysis-1"
SKILL_VERSION = "1"
REPORT_SECTIONS = ("问题回答：", "关键证据：", "关注重点：", "验证建议：", "分析局限：")


def _average(values):
    valid = [v for v in values if number(v)]
    return mean(valid) if valid else None


def _range(values):
    valid = [v for v in values if number(v)]
    return [min(valid), max(valid)] if valid else None


def _compact(value):
    if isinstance(value, float):
        return float(f"{value:.6g}")
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _samples(repository, user, workspace, row):
    result = []
    for record in repository.list_records("observations", user, workspace, row["runId"], limit=100000):
        sample = record.get("payload", record).get("observationMetrics")
        if isinstance(sample, dict) and number(sample.get("time_seconds")):
            result.append(sample)
    return result


def _cell_labels(geometry):
    cells = geometry.get("cells") or []
    rows = _range(c.get("row") for c in cells)
    columns = _range(c.get("column") for c in cells)
    labels = {}
    for cell in cells:
        row, column = cell.get("row"), cell.get("column")
        if not number(row) or not number(column) or not cell.get("cell_id"):
            continue
        vertical = (row - rows[0]) / max(1, rows[1] - rows[0])
        horizontal = (column - columns[0]) / max(1, columns[1] - columns[0])
        y = "南部" if vertical < 1 / 3 else "北部" if vertical > 2 / 3 else "中部"
        x = "偏西" if horizontal < 1 / 3 else "偏东" if horizontal > 2 / 3 else ""
        labels[cell["cell_id"]] = f"观测区域{y}{x}（第{row + 1}行、第{column + 1}列网格）"
    return labels


def _temporal(metric_id, runs):
    points, starts, ends, changes, per_run_extrema = [], [], [], [], []
    first_windows, last_windows = [], []
    for row, samples in runs:
        timed = [(s["time_seconds"], values_at(s).get(metric_id)) for s in samples]
        valid = [(t, v) for t, v in timed if number(v)]
        points.extend({"runId": row["runId"], "seed": row["seed"], "timeSeconds": t, "value": v} for t, v in valid)
        if not timed or not valid:
            continue
        start, end = timed[0][0], timed[-1][0]
        width = (end - start) / 3
        first = [(t, v) for t, v in valid if t <= start + width]
        last = [(t, v) for t, v in valid if t >= end - width]
        a, b = _average(v for _, v in first), _average(v for _, v in last)
        if a is not None and b is not None and end > start:
            starts.append(a); ends.append(b); changes.append(b - a)
            first_windows.append([start, start + width]); last_windows.append([end - width, end])
        per_run_extrema.append({"runId": row["runId"], "minimum": min(v for _, v in valid), "maximum": max(v for _, v in valid)})
    return {
        "metricId": metric_id, "label": METRICS[metric_id][0], "unit": METRICS[metric_id][1],
        "minimumSample": min(points, key=lambda p: p["value"]) if points else None,
        "maximumSample": max(points, key=lambda p: p["value"]) if points else None,
        "runMinimumRange": _range(r["minimum"] for r in per_run_extrema),
        "runMaximumRange": _range(r["maximum"] for r in per_run_extrema),
        "windowComparison": {
            "definition": "各轮实际观察窗口前1/3与后1/3的采样均值比较；不代表单调趋势或因果效应",
            "firstWindowSeconds": _range(t for window in first_windows for t in window),
            "lastWindowSeconds": _range(t for window in last_windows for t in window),
            "firstMean": _average(starts), "lastMean": _average(ends), "meanChange": _average(changes),
            "validRunCount": len(changes), "increasedRuns": sum(c > 1e-9 for c in changes),
            "decreasedRuns": sum(c < -1e-9 for c in changes), "unchangedRuns": sum(abs(c) <= 1e-9 for c in changes),
        },
    }


def _spatial(runs, labels, selected):
    cells, winners, boundary = defaultdict(list), Counter(), None
    regional_winners = defaultdict(set)
    for row, samples in runs:
        run_peaks = {}
        for sample in samples:
            for cell in sample.get("cells") or []:
                density = cell.get("density_person_per_m2")
                if "local-density" not in selected or not number(density) or not cell.get("cell_id"):
                    continue
                point = {"runId": row["runId"], "seed": row["seed"], "timeSeconds": sample["time_seconds"],
                         "density": density, "personCount": cell.get("person_count"), "areaM2": cell.get("area_m2"),
                         "sameCellSpeedMps": cell.get("avg_speed_mps") if "local-speed" in selected else None,
                         "speedSampleCount": cell.get("speed_sample_count") if "local-speed" in selected else None}
                cells[cell["cell_id"]].append(point)
                run_peaks[cell["cell_id"]] = max(run_peaks.get(cell["cell_id"], density), density)
            for item in sample.get("boundaries") or []:
                value = item.get("absolute_difference_person_per_m2")
                if ("boundary-density-difference" in selected and item.get("status") == "valid" and number(value)
                        and (boundary is None or value > boundary["absoluteDifference"])):
                    boundary = {"runId": row["runId"], "seed": row["seed"], "timeSeconds": sample["time_seconds"],
                                "boundaryId": item.get("boundary_id"), "cellA": item.get("cell_a"), "cellB": item.get("cell_b"),
                                "absoluteDifference": value, "unit": "人/㎡"}
        largest = max(run_peaks.values(), default=0)
        if largest > 0:
            peak_cells = [key for key, value in run_peaks.items() if abs(value - largest) <= 1e-9]
            winners.update(peak_cells)
            for key in peak_cells:
                if key in labels:
                    regional_winners[labels[key].split("（")[0]].add(row["runId"])
    ranked = []
    for cell_id, points in cells.items():
        peak = max(points, key=lambda p: p["density"])
        if peak["density"] <= 0:
            continue
        per_run = defaultdict(list)
        for point in points:
            per_run[point["runId"]].append(point["density"])
        ranked.append({"cellId": cell_id, "location": labels.get(cell_id, f"观测网格 {cell_id}"), "peakSample": peak,
                       "measuredRunCount": len(per_run), "runsWhereCellIsMaximum": winners[cell_id],
                       "region": labels[cell_id].split("（")[0] if cell_id in labels else None,
                       "runsWhereRegionIsMaximum": len(regional_winners[labels[cell_id].split("（")[0]]) if cell_id in labels else None,
                       "runPeakRange": _range(max(values) for values in per_run.values()),
                       "definition": "各轮采样最大密度落在此网格的轮数，允许并列；不是风险概率"})
    ranked.sort(key=lambda c: (-c["peakSample"]["density"], c["cellId"]))
    if boundary:
        boundary["locationA"] = labels.get(boundary["cellA"], boundary["cellA"])
        boundary["locationB"] = labels.get(boundary["cellB"], boundary["cellB"])
    return ranked[:3], boundary


def build_observation_analysis(repository, user, workspace, experiment, report):
    """Execute the skill's evidence tools against completed, valid archived runs."""
    binding = experiment["duringObservation"]
    requirement = binding["requirement"]
    selected = requirement["observation"]["metric_ids"]
    valid = [r for r in report["runs"] if r["valid"]]
    runs = [(r, _samples(repository, user, workspace, r)) for r in valid]
    records = {r["run_id"]: r for r in repository.list_runs(user, workspace, experiment["experiment_id"])}
    geometry = next(((records.get(r["runId"], {}).get("observationBinding") or {}).get("geometry") or {}
                     for r in valid), {})
    evidence = []

    def add(kind, data):
        item = {"id": f"E{len(evidence) + 1:02d}", "kind": kind, **data}
        evidence.append(item)
        return item["id"]

    quality_id = add("data-quality", {
        "validRuns": len(valid), "plannedRuns": report["expectedRunCount"],
        "partial": report["experimentStatus"] not in {"completed", "completed_with_failures", "failed", "cancelled"},
        "excluded": [{"runId": r["runId"], "status": r["status"], "reason": r["excludedReason"]}
                     for r in report["runs"] if not r["valid"]],
        "samplingIntervalSeconds": report["observationIntervalSeconds"],
        "replicationAssessment": "仅单轮，不能判断跨轮一致性" if len(valid) < 2 else "仅描述当前独立种子之间的差异，不作总体概率或显著性判断",
    })
    windows = []
    for row, samples in runs:
        if not samples:
            continue
        activity = row.get("hotspotActivity")
        legacy = not activity
        activity = activity or {"start_seconds": 600, "end_seconds": 800}
        start, end = activity.get("start_seconds"), activity.get("end_seconds")
        last = samples[-1]["time_seconds"]
        assessment = "活动结束时间未设定，不能评价完整离场过程" if end is None else "已覆盖活动结束时点，但不等于全部行程已完成"
        if number(start) and last <= start:
            assessment = "观察止于活动开始时点或之前，未覆盖活动开始后的完整过程；不能据此否定此前已出现的局部聚集"
        elif number(end) and last < end:
            assessment = "观察尚未覆盖活动结束时点，不能评价完整离场过程"
        windows.append({"runId": row["runId"], "seed": row["seed"], "firstSeconds": samples[0]["time_seconds"],
                        "lastSeconds": last, "activityStartSeconds": start, "activityEndSeconds": end,
                        "activitySource": "旧600–800秒预设，缺少确认时间执行绑定" if legacy else "本轮已核验的活动执行绑定",
                        "assessment": assessment})
    window_id = add("observation-window", {"runs": windows})
    metric_ids = []
    for key in selected:
        if key in METRICS:
            metric_ids.append(add("temporal-metric", _temporal(key, runs)))
    person_rows = [{"runId": row["runId"], "first": row.get("personCount", {}).get("first"),
                    "last": row.get("personCount", {}).get("last")} for row in valid]
    add("population-in-scope", {"runs": person_rows, "definition": "观测范围内人数变化，不代表安全疏散完成或策略效果"})
    hotspots, boundary = _spatial(runs, _cell_labels(geometry), selected)
    spatial_ids = [add("spatial-hotspot", item) for item in hotspots]
    if boundary:
        add("boundary-contrast", boundary)
    limitations = [
        "仅为当前模型、配置和种子下的仿真实测，不是现实监测结论；无干预对照，不作因果或措施效果判断。",
        "采样峰值不代表连续时间峰值；初末段均值差不代表单调趋势；缺失速度不等于零。",
        "密度按观测多边形/裁切网格面积计算，并非道路有效通行面积；须结合网格面积和人数解读，不套用未配置安全阈值。",
        "相对高密度或低速不足以证明拥堵；活动等待也可能低速；边界密度差不是流量、方向或压力。",
        "措施仅能作为待验证建议，不能编造可执行入口、资源、限流比例或改善幅度。",
    ]
    if not hotspots:
        limitations.append("缺少已选局部密度或可用分区观测，不定位空间热点，不猜造道路或瓶颈。")
    result = _compact({
        "schemaVersion": ANALYSIS_VERSION, "experimentId": report["experimentId"],
        "executionVersion": report["executionVersion"], "observationEvidenceVersion": report.get("observationEvidenceVersion"),
        "study": {"title": requirement.get("project", {}).get("title", ""),
                  "researchQuestion": requirement.get("project", {}).get("description", ""),
                  "location": requirement.get("spatial_scope", {}).get("name", ""),
                  "population": experiment["scenario"]["population"], "durationSeconds": experiment["scenario"]["durationSeconds"],
                  "selectedMetricIds": selected, "densityAreaBasis": geometry.get("density_area_basis"),
                  "scopeAreaM2": geometry.get("area_m2")},
        "evidence": evidence, "focusEvidenceIds": [window_id, *spatial_ids[:1], *metric_ids, quality_id],
        "limitations": limitations,
        "executedSteps": ["validate_completed_evidence", "compare_observation_window", "analyze_sampled_time_series",
                          "compare_independent_runs", "locate_same_cell_hotspots"],
    })
    result["evidenceFingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def analysis_skill(kind):
    """Load the actual project skill with the existing skill loader."""
    definition = load_skill(Path(__file__).resolve().parents[2] / "skills" / "during_experiment_analysis")
    contract = (
        "本次产物为一句话推演结论：围绕研究问题，选一个主要发现并说明其含义，输出一个单行句子，目标80–120字、最多200字；"
        "只写结论正文，不写“问题回答”“结论”等标题，不附建议，不罗列其他指标；可用分号连接必要的观察窗口限制，不列举、不分段、不附报告编号。"
        if kind == "conclusion" else
        "本次产物为报告解读：按以下五个标签各写一段，严格保留标签：" + "、".join(REPORT_SECTIONS) +
        "；各段之间换行，总长目标500–700字，最多3条重点发现、2条待验证建议，每条带数值的关键发现用完整证据编号（如[E04]）引用给定证据，"
        "不能只列均值或重复统计表，不展示实验编号和版本哈希；建议须说明针对何处、何时、如何验证，证据不足则明确下一步应补什么观测。"
    )
    return {"name": definition.name, "version": SKILL_VERSION, "instruction": definition.build_prompt(mode="plan", phase="during") + "\n\n" + contract}
