"""Compute versioned metrics only from a completed SUMO run's raw evidence.

Expected evidence is produced by a future SUMO batch adapter, never by the
browser: fixed effective cell areas, ordered frame times, stable person IDs,
positions/velocities, and optional safe-zone/cost evidence.
"""

from __future__ import annotations

import math
from typing import Any

from algorithm.metrics import evacuation_time, kernel_crowd_pressure, mean_movement_speed, peak_grid_density
from postanalysis_api.schemas.common import ApiError, metric_value
from postanalysis_api.schemas.metrics import METRICS, METRIC_VERSION, RISK_LEVELS, RISK_RULE_VERSION


def _risk_level(density: float) -> str:
    if density < 1.0:
        return "low"
    if density < 2.0:
        return "medium"
    if density < 3.5:
        return "medium_high"
    return "high"


def calculate_run_metrics(evidence: dict[str, Any], metric_ids: list[str]) -> dict:
    """Return scalar metrics plus exposure by all four model risk levels.

    A frame describes the *left* endpoint of the interval to the next frame.
    `cells[cellId]` contains `personIds`; `people` has matching `id`,
    `position`, and `velocity` for every active person. Cell mean speed is
    calculated from those people, never supplied by the client.
    """
    if any(metric not in METRICS for metric in metric_ids):
        raise ApiError("METRIC_UNSUPPORTED", "请求包含未知指标", 422)
    frames = evidence.get("frames") or []
    areas = evidence.get("effectiveAreas") or {}
    if not isinstance(frames, list) or not isinstance(areas, dict):
        raise ApiError("RUN_EVIDENCE_INVALID", "轨迹帧或有效面积格式错误", 422)
    times = [frame.get("timeSeconds") for frame in frames]
    if any(isinstance(time, bool) or not isinstance(time, (int, float)) or not math.isfinite(time) for time in times):
        raise ApiError("RUN_EVIDENCE_INVALID", "仿真时间必须是有限数值", 422)
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ApiError("TIME_ORDER_INVALID", "轨迹帧时间必须严格递增", 422)
    if any(isinstance(area, bool) or not isinstance(area, (int, float)) or
           not math.isfinite(area) or not 0 < area <= 25 for area in areas.values()):
        raise ApiError("AREA_UNMAPPED", "网格有效面积必须在 (0,25] m²", 422)

    snapshots = []
    speed_samples = []
    pressure_values = []
    exposed_ids = {level["id"]: set() for level in RISK_LEVELS}
    exposed_seconds = {level["id"]: 0.0 for level in RISK_LEVELS}
    congestion_flags = []
    points = evidence.get("pressureEvaluationPoints") or []
    radius = evidence.get("pressureKernelRadiusMeters")

    for index, frame in enumerate(frames):
        cells = frame.get("cells") or {}
        people = frame.get("people") or []
        if not isinstance(cells, dict) or not isinstance(people, list):
            raise ApiError("RUN_EVIDENCE_INVALID", "网格或人员轨迹格式错误", 422)
        if any(not isinstance(person, dict) or not isinstance(person.get("id"), str) or not person["id"] for person in people):
            raise ApiError("RUN_EVIDENCE_INVALID", "每个人员轨迹必须有非空 ID", 422)
        by_id = {person["id"]: person for person in people}
        if len(by_id) != len(people):
            raise ApiError("RUN_EVIDENCE_INVALID", "同一轨迹帧人员 ID 重复", 422)
        counts = {}
        seen_in_cells = set()
        congested_now = False
        dt = times[index + 1] - times[index] if index + 1 < len(frames) else 0.0
        velocities = []
        for person in people:
            velocity = person.get("velocity")
            if not isinstance(velocity, (list, tuple)) or len(velocity) != 2:
                raise ApiError("RUN_EVIDENCE_INVALID", "人员速度必须为二维向量", 422)
            velocities.append(velocity)
        if dt:
            speed_samples.append({"dt": dt, "speeds": velocities})
        for cell_id, ids in cells.items():
            if cell_id not in areas:
                raise ApiError("AREA_UNMAPPED", "轨迹包含未配置有效面积的网格", 422)
            if not isinstance(ids, list) or any(not isinstance(person_id, str) for person_id in ids) or len(set(ids)) != len(ids) or any(person_id not in by_id for person_id in ids):
                raise ApiError("RUN_EVIDENCE_INVALID", "网格人员 ID 与轨迹不一致", 422)
            if seen_in_cells.intersection(ids):
                raise ApiError("RUN_EVIDENCE_INVALID", "同一人员不能同时出现在多个网格", 422)
            seen_in_cells.update(ids)
            counts[cell_id] = len(ids)
            density = len(ids) / areas[cell_id]
            level = _risk_level(density)
            exposed_ids[level].update(ids)
            exposed_seconds[level] += len(ids) * dt
            if ids:
                mean_speed = sum(math.hypot(*by_id[person_id]["velocity"]) for person_id in ids) / len(ids)
                congested_now |= density >= 3.5 and mean_speed <= 0.5
        snapshots.append(counts)
        congestion_flags.append(congested_now)
        if points and radius is not None:
            for point in points:
                value = kernel_crowd_pressure(people, tuple(point), radius)["P_crowd"]
                if value is not None:
                    pressure_values.append(value)

    congestion_duration = 0.0
    episode = 0.0
    for index in range(max(0, len(frames) - 1)):
        dt = times[index + 1] - times[index]
        if congestion_flags[index]:
            episode += dt
        else:
            if episode >= 10:
                congestion_duration += episode
            episode = 0.0
    if episode >= 10:
        congestion_duration += episode

    raw_values = {
        "peak_density": peak_grid_density(snapshots, areas) if frames and areas else None,
        "mean_speed": mean_movement_speed(speed_samples) if speed_samples else None,
        "peak_pressure_proxy": max(pressure_values) if pressure_values else None,
        "risk_exposed_unique": len(exposed_ids["high"]) if frames and areas else None,
        "risk_exposure": exposed_seconds["high"] if len(frames) >= 2 and areas else None,
        "congestion_duration": congestion_duration if len(frames) >= 2 and areas else None,
        "evacuation_time": None,
        "resource_cost": None,
    }
    if evidence.get("targetPersonIds") and evidence.get("safeArrivalTimes") is not None and evidence.get("evacuationStartSeconds") is not None and times:
        outcome = evacuation_time(evidence["targetPersonIds"], evidence["safeArrivalTimes"],
                                  evidence["evacuationStartSeconds"], times[-1])
        raw_values["evacuation_time"] = outcome["T_evac"]
    if evidence.get("resourceUsage") is not None and evidence.get("costTable"):
        usage, prices = evidence["resourceUsage"], evidence["costTable"]
        if set(usage) <= set(prices) and evidence.get("costTableVersion"):
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                   not math.isfinite(value) or value < 0 for value in [*usage.values(), *prices.values()]):
                raise ApiError("RUN_EVIDENCE_INVALID", "资源量与单价必须为非负有限数值", 422)
            raw_values["resource_cost"] = sum(float(amount) * float(prices[key]) for key, amount in usage.items())

    missing = {
        "peak_density": "AREA_UNMAPPED" if not areas else "NO_TRAJECTORY",
        "mean_speed": "NO_TRAJECTORY",
        "peak_pressure_proxy": "NO_KERNEL_CONFIG" if not points or radius is None else "NO_TRAJECTORY",
        "risk_exposed_unique": "AREA_UNMAPPED" if not areas else "NO_TRAJECTORY",
        "risk_exposure": "INSUFFICIENT_TIME_POINTS",
        "congestion_duration": "INSUFFICIENT_TIME_POINTS",
        "evacuation_time": "NO_SAFE_ZONE_OR_CENSORED",
        "resource_cost": "NO_VERSIONED_COST_TABLE",
    }
    metrics = {metric: metric_value(raw_values[metric], METRICS[metric]["unit"], METRIC_VERSION,
                                    missing_reason=missing[metric] if raw_values[metric] is None else None)
               for metric in metric_ids}
    return {"definitionVersion": METRIC_VERSION, "riskRuleVersion": RISK_RULE_VERSION,
            "metrics": metrics,
            "riskByLevel": {level: {"uniquePersons": len(exposed_ids[level]) if frames and areas else None,
                                   "personSeconds": exposed_seconds[level] if len(frames) >= 2 else None,
                                   "unit": {"uniquePersons": "person", "personSeconds": "person*s"},
                                   "missingReason": None if frames and areas else "NO_TRAJECTORY_OR_AREA"}
                            for level in exposed_ids}}
