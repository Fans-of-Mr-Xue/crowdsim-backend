"""Generic factual/counterfactual metric comparison and score aggregation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from ._inference import paired_summary


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def compare_metrics(
    baseline: Mapping[str, float], candidate: Mapping[str, float],
    definitions: Sequence[Mapping],
) -> list[dict]:
    """Compare like-for-like metrics using their preferred direction.

    ``improvementPct`` is positive when the candidate improves. A zero
    baseline has no defined percentage change and returns None for that field.
    Missing values also return None rather than an invented result.
    """
    comparisons = []
    for definition in definitions:
        key = definition["key"]
        direction = definition.get("direction", "down")
        if direction not in {"up", "down"}:
            raise ValueError(f"unsupported direction for {key}: {direction}")
        left, right = baseline.get(key), candidate.get(key)
        available = left is not None and right is not None
        if available:
            factual = _number(left, f"baseline.{key}")
            counterfactual = _number(right, f"candidate.{key}")
            difference = counterfactual - factual
            improvement = difference if direction == "up" else -difference
            improvement_pct = improvement / abs(factual) * 100 if factual else None
        else:
            factual = counterfactual = difference = improvement = improvement_pct = None
        comparisons.append({
            "key": key,
            "label": definition.get("label", key),
            "unit": definition.get("unit", ""),
            "direction": direction,
            "available": available,
            "baseline": factual,
            "candidate": counterfactual,
            "change": difference,
            "improvement": improvement,
            "improvementPct": improvement_pct,
        })
    return comparisons


def weighted_score(scores: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """Return a weighted mean using only explicitly supplied score dimensions."""
    if not weights:
        raise ValueError("weights must not be empty")
    denominator = 0.0
    numerator = 0.0
    for key, raw_weight in weights.items():
        weight = _number(raw_weight, f"weight.{key}")
        if weight < 0:
            raise ValueError("weights must not be negative")
        score = _number(scores[key], f"score.{key}")
        numerator += score * weight
        denominator += weight
    if denominator <= 0:
        raise ValueError("at least one weight must be positive")
    return numerator / denominator


def aggregation_start(bins: Sequence[Mapping], threshold: float, minimum_duration: float) -> float | None:
    """首个持续越阈值时段；每个 bin 含 start、end、count。"""
    limit = _number(threshold, "threshold")
    duration = _number(minimum_duration, "minimum_duration")
    if duration < 0:
        raise ValueError("minimum_duration must not be negative")
    episode_start = episode_end = None
    previous_end = None
    for item in sorted(bins, key=lambda row: row["start"]):
        start, end = _number(item["start"], "start"), _number(item["end"], "end")
        if end <= start:
            raise ValueError("every bin must have positive duration")
        if previous_end is not None and start < previous_end:
            raise ValueError("aggregation bins must not overlap")
        previous_end = end
        count = _number(item["count"], "count")
        if count < 0:
            raise ValueError("count must not be negative")
        if count >= limit:
            if episode_start is None or start > episode_end:
                episode_start = start
            episode_end = end
            if episode_end - episode_start >= duration:
                return episode_start
        else:
            episode_start = episode_end = None
    return None


def population_factors(target_people: Sequence[Mapping], age_threshold: float) -> dict:
    """总人数与老年人占比；空目标人群的占比为 None。"""
    threshold = _number(age_threshold, "age_threshold")
    identifiers = [person["id"] for person in target_people]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("target person ids must be unique")
    count = len(target_people)
    elderly = sum(_number(person["age"], "age") >= threshold for person in target_people)
    return {"N_crowd": count, "R_elderly": elderly / count if count else None}


def active_population_counts(active_ids_by_time: Sequence[Sequence[str]]) -> dict:
    """各时点同时在场人数与峰值，区别于目标人群总数。"""
    counts = [len(set(identifiers)) for identifiers in active_ids_by_time]
    return {"counts": counts, "N_peak": max(counts) if counts else None}


def arrival_rates(entry_times: Sequence[float], bins: Sequence[tuple[float, float]]) -> list[dict]:
    """按 [start,end) 统计实际入口跨越次数，单位人/秒。"""
    times = [_number(time, "entry_time") for time in entry_times]
    result = []
    for start_raw, end_raw in bins:
        start, end = _number(start_raw, "start"), _number(end_raw, "end")
        if end <= start:
            raise ValueError("arrival bin must have positive duration")
        count = sum(start <= time < end for time in times)
        result.append({"start": start, "end": end, "count": count, "rate": count / (end - start)})
    return result


def gate_capacity(gates: Sequence[Mapping]) -> dict:
    """按已校准饱和比流量与有效宽度计算各闸口容量。"""
    widths = {}
    capacities = {}
    for gate in gates:
        gate_id = gate["id"]
        if gate_id in widths:
            raise ValueError("gate ids must be unique")
        geometric = _number(gate["geometric_width"], "geometric_width")
        blocked = _number(gate.get("blocked_width", 0), "blocked_width")
        unusable = _number(gate.get("unusable_width", 0), "unusable_width")
        if min(geometric, blocked, unusable) < 0:
            raise ValueError("gate widths must not be negative")
        width = max(0.0, geometric - blocked - unusable)
        flow = _number(gate["saturated_specific_flow"], "saturated_specific_flow")
        if flow < 0:
            raise ValueError("specific flow must not be negative")
        widths[gate_id] = width
        capacities[gate_id] = width * flow
    return {"effective_widths": widths, "gate_capacities": capacities,
            "C_gate": sum(capacities.values())}


def bottleneck_width(path_gate_ids: Sequence[str], effective_widths: Mapping[str, float]) -> float:
    if not path_gate_ids:
        raise ValueError("path must contain at least one edge")
    return min(_number(effective_widths[gate_id], "effective_width") for gate_id in path_gate_ids)


def response_delays(records: Sequence[Mapping]) -> dict:
    """未响应者只计入比例，绝不作为零延迟。"""
    delays = []
    for record in records:
        received = record.get("received_time")
        response = record.get("response_time")
        if received is None or response is None:
            continue
        delay = _number(response, "response_time") - _number(received, "received_time")
        if delay < 0:
            raise ValueError("response precedes receipt")
        delays.append(delay)
    ordered = sorted(delays)
    if not records:
        return {"mean": None, "median": None, "p90": None, "nonresponse_fraction": None}
    def quantile(p: float) -> float | None:
        if not ordered:
            return None
        index = (len(ordered) - 1) * p
        low = math.floor(index)
        return ordered[low] * (1 - index + low) + ordered[min(low + 1, len(ordered) - 1)] * (index - low)
    return {"mean": sum(delays) / len(delays) if delays else None,
            "median": quantile(0.5), "p90": quantile(0.9),
            "nonresponse_fraction": (len(records) - len(delays)) / len(records)}


def normalized_intensity(value: float, minimum: float, maximum: float) -> float:
    value, low, high = _number(value, "value"), _number(minimum, "minimum"), _number(maximum, "maximum")
    if high <= low or not low <= value <= high:
        raise ValueError("intensity requires minimum <= value <= maximum and maximum > minimum")
    return (value - low) / (high - low)


def information_coverage(eligible_ids: Sequence[str], received_ids: Sequence[str]) -> float | None:
    eligible = set(eligible_ids)
    if not eligible:
        return None
    return len(eligible.intersection(received_ids)) / len(eligible)


def peak_grid_density(snapshots: Sequence[Mapping[str, int]], effective_areas: Mapping[str, float]) -> float | None:
    """固定网格的时空最大计数密度；无时间窗返回 None。"""
    if not snapshots:
        return None
    if not effective_areas:
        raise ValueError("effective_areas must not be empty")
    peak = 0.0
    for counts in snapshots:
        for cell_id, raw_area in effective_areas.items():
            area = _number(raw_area, "effective_area")
            count = _number(counts.get(cell_id, 0), "cell_count")
            if area <= 0 or count < 0 or not count.is_integer():
                raise ValueError("effective areas must be positive and counts nonnegative integers")
            peak = max(peak, count / area)
    return peak


def mean_movement_speed(samples: Sequence[Mapping]) -> float | None:
    """人时加权速度模长；零速人员计入分母。"""
    numerator = denominator = 0.0
    for sample in samples:
        dt = _number(sample["dt"], "dt")
        if dt <= 0:
            raise ValueError("dt must be positive")
        speeds = sample.get("speeds", [])
        for velocity in speeds:
            if isinstance(velocity, (int, float)):
                speed = _number(velocity, "speed")
            else:
                components = [_number(component, "velocity") for component in velocity]
                if len(components) != 2:
                    raise ValueError("velocity vector must have two components")
                speed = math.hypot(*components)
            if speed < 0:
                raise ValueError("speed must be nonnegative")
            numerator += dt * speed
        denominator += dt * len(speeds)
    return numerator / denominator if denominator else None


def max_contact_pressure(samples: Sequence[Sequence[Mapping]], stiffness: float) -> float | None:
    """二维圆盘模型身体接触力之和除以周长，单位 N/m。"""
    k = _number(stiffness, "stiffness")
    if k < 0:
        raise ValueError("stiffness must be nonnegative")
    if not samples:
        return None
    peak = 0.0
    for people in samples:
        for i, person in enumerate(people):
            radius = _number(person["radius"], "radius")
            if radius <= 0:
                raise ValueError("radius must be positive")
            x, y = [_number(v, "position") for v in person["position"]]
            force = 0.0
            for j, other in enumerate(people):
                if i == j:
                    continue
                ox, oy = [_number(v, "position") for v in other["position"]]
                overlap = radius + _number(other["radius"], "radius") - math.hypot(x - ox, y - oy)
                force += k * max(0.0, overlap)
            for distance in person.get("wall_distances", ()):
                force += k * max(0.0, radius - _number(distance, "wall_distance"))
            peak = max(peak, force / (2 * math.pi * radius))
    return peak


def kernel_crowd_pressure(people: Sequence[Mapping], point: tuple[float, float], radius: float) -> dict:
    """核密度乘加权速度方差，与机械接触压力分开记录。"""
    kernel_radius = _number(radius, "radius")
    if kernel_radius <= 0:
        raise ValueError("kernel radius must be positive")
    px, py = map(lambda value: _number(value, "point"), point)
    weighted = []
    for person in people:
        x, y = [_number(value, "position") for value in person["position"]]
        vx, vy = [_number(value, "velocity") for value in person["velocity"]]
        distance_sq = (x - px) ** 2 + (y - py) ** 2
        weight = math.exp(-distance_sq / kernel_radius ** 2) / (math.pi * kernel_radius ** 2)
        weighted.append((weight, vx, vy))
    density = sum(row[0] for row in weighted)
    if density == 0:
        return {"density": 0.0, "velocity_variance": None, "P_crowd": None}
    mean_vx = sum(w * vx for w, vx, _ in weighted) / density
    mean_vy = sum(w * vy for w, _, vy in weighted) / density
    variance = sum(w * ((vx - mean_vx) ** 2 + (vy - mean_vy) ** 2)
                   for w, vx, vy in weighted) / density
    return {"density": density, "velocity_variance": variance, "P_crowd": density * variance}


def max_kernel_crowd_pressure(samples: Sequence[Sequence[Mapping]],
                              evaluation_points: Sequence[tuple[float, float]],
                              radius: float) -> float | None:
    """对固定观测点和时间快照取人群压力指标的最大值。"""
    if not samples or not evaluation_points:
        return None
    return max(kernel_crowd_pressure(people, point, radius)["P_crowd"] or 0.0
               for people in samples for point in evaluation_points)


def evacuation_time(target_ids: Sequence[str], safe_times: Mapping[str, float],
                    start: float, horizon: float) -> dict:
    """未完成时报告右删失和下界，不把观察终点当完成时间。"""
    t0, end = _number(start, "start"), _number(horizon, "horizon")
    if end < t0:
        raise ValueError("horizon must follow start")
    population = set(target_ids)
    if len(population) != len(target_ids):
        raise ValueError("target ids must be unique")
    if not population:
        return {"T_evac": None, "censored": None, "lower_bound": None, "completion_fraction": None}
    completed = {person_id: max(t0, _number(safe_times[person_id], "safe_time")) for person_id in population
                 if person_id in safe_times and safe_times[person_id] is not None
                 and _number(safe_times[person_id], "safe_time") <= end}
    all_safe = len(completed) == len(population)
    return {"T_evac": max(completed.values()) - t0 if all_safe else None,
            "censored": not all_safe, "lower_bound": None if all_safe else end - t0,
            "completion_fraction": len(completed) / len(population)}


def paired_metric_effect(baseline_runs: Sequence[float], candidate_runs: Sequence[float], **kwargs) -> dict:
    """按完整重复运行配对；删失疏散时间不得传入普通均值差。"""
    return paired_summary(baseline_runs, candidate_runs, **kwargs)
