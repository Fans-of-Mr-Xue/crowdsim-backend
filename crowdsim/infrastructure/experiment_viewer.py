"""Read frozen observation files for an offline viewer, without importing SUMO.

The snapshot index is the commit boundary. Incomplete appends are never made
visible, missing values stay null, and a closed run is not an evacuation.
"""

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET


METRICS = [
    ("A1", "global-density", "全局密度", "人/m²"),
    ("A2", "local-density", "局部密度", "人/m²"),
    ("A3", "global-speed", "全局速度", "m/s"),
    ("A4", "local-speed", "局部速度", "m/s"),
    ("A5", "boundary-density-difference", "边界密度差", "人/m²"),
    ("B1", "evacuation-time", "疏散时间", "仿真秒"),
    ("B2", "evacuation-efficiency", "疏散效率", "人/(m²·仿真秒)"),
    ("B3", "absolute-evacuation-density", "人群密度差", "人/m²"),
    ("C1", "pedestrian-state-change", "行为状态变化", "人"),
    ("C2", "pedestrian-psychology-change", "心理变化", "人 / 压力 0–1"),
]
SCENES = {"memorial-tower": "黄浦公园—纪念塔", "east-nanjing-road": "南京东路—外滩"}
GLOBAL_FIELDS = (
    "person_count", "network_person_count", "outside_scope_person_count", "invalid_position_count",
    "area_m2", "density_person_per_m2", "speed_sample_count", "invalid_speed_count", "avg_speed_mps",
    "moving_person_count", "moving_avg_speed_mps", "behavior_walking_count", "behavior_waiting_count",
    "behavior_blocked_count", "behavior_avoiding_count", "behavior_unknown_count", "crowded_person_count",
    "crowding_unknown_count", "psychology_calm_count", "psychology_tense_count", "psychology_panic_count",
    "psychology_unknown_count", "stress_avg", "fatigue_avg", "perceived_risk_avg", "perceived_crowding_avg",
)
DEFAULT_SCOPE_REFERENCES = Path(__file__).resolve().parents[2] / "config/viewer_scope_references.json"


def number(value):
    """Empty, malformed and nonfinite numbers are observations of no value."""
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _canonical_ring(ring, digits):
    if not isinstance(ring, (list, tuple)):
        return None
    points = []
    for point in ring:
        if not isinstance(point, (list, tuple)) or len(point) < 2 or any(number(value) is None for value in point[:2]):
            return None
        points.append(tuple(round(float(value), digits) for value in point[:2]))
    if points and points[0] == points[-1]:
        points.pop()
    if len(points) < 3:
        return None
    # Closing point, starting corner and winding direction do not define a new scope.
    rotations = []
    for ordered in (points, list(reversed(points))):
        smallest = min(ordered)
        for index, point in enumerate(ordered):
            if point == smallest:
                rotations.append(ordered[index:] + ordered[:index])
    return min(rotations)


def scope_version(boundary, *, xy=False):
    if not isinstance(boundary, dict) or boundary.get("type") != "Polygon":
        return None
    coordinates = boundary.get("coordinates")
    if not isinstance(coordinates, (list, tuple)):
        return None
    rings = [_canonical_ring(ring, 3 if xy else 8) for ring in coordinates]
    if not rings or any(ring is None for ring in rings):
        return None
    encoded = json.dumps([rings[0], *sorted(rings[1:])], separators=(",", ":"))
    return ("xy-" if xy else "scope-") + hashlib.sha256(encoded.encode()).hexdigest()[:12]


def _vertex_count(boundary):
    if not isinstance(boundary, dict) or boundary.get("type") != "Polygon":
        return None
    ring = (boundary.get("coordinates") or [[]])[0]
    return len(ring) - int(bool(ring) and ring[0] == ring[-1])


def _scope_info(geometry, spatial_scope, reference):
    boundary = geometry.get("source_boundary") or spatial_scope.get("boundary")
    version = scope_version(boundary)
    info = {"version": version or scope_version(geometry.get("scope_geometry"), xy=True),
            "vertex_count": _vertex_count(boundary), "status": "no_reference", "reference": None,
            "area_delta_m2": None, "overlay_geometry": None}
    if not reference:
        return info
    reference_version = scope_version(reference.get("boundary"))
    info.update(status="matching" if version and version == reference_version else "changed" if version else "unavailable",
                reference={"version": reference_version, "name": reference.get("name"),
                           "revision": reference.get("revision"), "source": reference.get("source"),
                           "vertex_count": _vertex_count(reference.get("boundary")),
                           "area_m2": number(reference.get("area_m2"))})
    recorded_area, reference_area = number(geometry.get("area_m2")), number(reference.get("area_m2"))
    if recorded_area is not None and reference_area is not None:
        info["area_delta_m2"] = reference_area - recorded_area
    old_projection, new_projection = geometry.get("projection") or {}, reference.get("projection") or {}
    old_offset = old_projection.get("net_offset_xy") or []
    new_offset = new_projection.get("net_offset_xy") or []
    if (old_projection.get("proj_parameter") and old_projection.get("proj_parameter") == new_projection.get("proj_parameter")
            and len(old_offset) == len(new_offset) == 2 and all(number(v) is not None for v in old_offset + new_offset)):
        dx, dy = float(old_offset[0]) - float(new_offset[0]), float(old_offset[1]) - float(new_offset[1])
    elif (not old_projection or not new_projection) and geometry.get("network_sha256") and geometry.get("network_sha256") == reference.get("network_sha256"):
        dx, dy = 0, 0
    else:
        return info
    source_geometry = reference.get("scope_geometry") or {}
    if scope_version(source_geometry, xy=True):
        info["overlay_geometry"] = {"type": "Polygon", "coordinates": [
            [[float(point[0]) + dx, float(point[1]) + dy] for point in ring] for ring in source_geometry["coordinates"]]}
    return info


def _load_scope_references(path):
    if path is None:
        path = DEFAULT_SCOPE_REFERENCES
        if not path.is_file():
            return {}
    config = _json(Path(path), Counter(), required=True)
    references = config.get("references")
    if not isinstance(references, dict):
        raise ValueError("范围对照文件必须包含 references 对象")
    for scene, reference in references.items():
        if (not isinstance(reference, dict) or not scope_version(reference.get("boundary"))
                or number(reference.get("area_m2")) is None or number(reference.get("area_m2")) <= 0):
            raise ValueError(f"范围对照文件包含无效边界或面积：{scene}")
    return references


def _json(path, issues, required=False):
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("expected an object")
        return _clean(result)
    except (OSError, ValueError) as error:
        if required:
            raise ValueError(f"{path.name}: {error}") from error
        if path.exists():
            issues["损坏或无法读取的 JSON 文件"] += 1
        return {}


def _jsonl(path, issues):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("expected an object")
                yield _clean(row)
            except ValueError:
                issues[f"{path.name} 无效或未写完的行"] += 1


def _rows(path, committed, issues, key=None):
    """Deduplicate retries by (snapshot, entity), taking the last committed row."""
    result = defaultdict(dict)
    if not path.exists():
        return result
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            sid = row.get("snapshot_id")
            if sid not in committed:
                issues[f"{path.name} 未提交行（已忽略）"] += 1
                continue
            if number(row.get("time_seconds")) != committed[sid]["time_seconds"]:
                issues[f"{path.name} 时间不匹配行（已忽略）"] += 1
                continue
            entity = row.get(key) if key else "global"
            if not entity:
                issues[f"{path.name} 缺少编号行（已忽略）"] += 1
                continue
            if entity in result[sid]:
                issues[f"{path.name} 重复行（已合并）"] += 1
            # Keep compact numeric columns; do not retain large CSV string dicts.
            fields = GLOBAL_FIELDS if key is None else ("density_person_per_m2", "avg_speed_mps", "person_count") if key == "cell_id" else ("difference_person_per_m2",)
            result[sid][entity] = {field: number(row.get(field)) for field in fields}
            if key == "boundary_id":
                result[sid][entity]["status"] = row.get("status")
    return result


def _sample_indices(size, limit, required=()):
    if size <= limit:
        return list(range(size))
    # Preserve event and intervention baselines even when spatial maps are thinned.
    indices = {0, size - 1, *required}
    remaining = max(0, limit - len(indices))
    indices.update(round((size - 1) * (i + 1) / (remaining + 1)) for i in range(remaining))
    return sorted(indices)


def _transition_data(directory, committed, issues):
    by_frame = defaultdict(Counter)
    frame_matrices = defaultdict(lambda: {"behavior_state": Counter(), "psychological_state": Counter()})
    seen = set()
    counts = Counter()
    for row in _jsonl(directory / "state_transitions.jsonl", issues):
        sid = row.get("snapshot_id")
        if sid not in committed:
            issues["state_transitions.jsonl 未提交行（已忽略）"] += 1
            continue
        if number(row.get("time_seconds")) != committed[sid]["time_seconds"]:
            issues["state_transitions.jsonl 时间不匹配行（已忽略）"] += 1
            continue
        signature = (sid, row.get("person_id"), row.get("dimension"), str(row.get("from_state")), str(row.get("to_state")), row.get("reason"))
        if signature in seen:
            issues["state_transitions.jsonl 重复事件（已合并）"] += 1
            continue
        seen.add(signature)
        counts[sid] += 1
        dimension, before, after = row.get("dimension"), row.get("from_state"), row.get("to_state")
        if before is None:
            by_frame[sid]["initial"] += 1
        elif dimension in {"behavior_state", "psychological_state"} and before != after:
            by_frame[sid][dimension] += 1
            frame_matrices[sid][dimension][(str(before), str(after))] += 1
        elif dimension == "presence":
            # First-frame scope membership is initialization, not a scope flow.
            if committed[sid]["first"]:
                by_frame[sid]["initial_presence"] += 1
            elif before != "inside" and after == "inside":
                by_frame[sid]["scope_entry"] += 1
            elif before == "inside" and after != "inside":
                by_frame[sid]["scope_exit"] += 1
    for sid, frame in committed.items():
        if number(frame.get("transition_rows")) != counts[sid]:
            issues["转换事件数与提交索引不一致的快照"] += 1
            by_frame[sid] = None
    matrices = {"behavior_state": Counter(), "psychological_state": Counter()}
    for sid, frame in frame_matrices.items():
        if by_frame[sid] is not None:
            for key in matrices:
                matrices[key].update(frame[key])
    return by_frame, {key: [[a, b, count] for (a, b), count in sorted(value.items())]
                      for key, value in matrices.items()}


def _evacuation(directory, result, committed, samples, cells, cell_ids, issues):
    state = _json(directory / "evacuation_state.json", issues)
    source = "recorded" if state else "legacy"
    state = state or dict(result.get("evacuation") or {})
    t0 = number(state.get("event_start_time_seconds"))
    ta = number(state.get("strategy_applied_time_seconds"))
    te = number(state.get("completion_time_seconds"))
    by_id = {sample["snapshot_id"]: sample for sample in samples}
    by_time = {sample["time_seconds"]: sample for sample in samples}

    def baseline(name, time):
        frozen = state.get(name) or {}
        sid = frozen.get("snapshot_id")
        sample = by_id.get(sid) if sid else by_time.get(time)
        if sample is None or time is None or sample["time_seconds"] != time:
            return None, None
        if frozen.get("valid") is False or sample["invalid_position_count"] != 0:
            return None, None
        rows = cells.get(sample["snapshot_id"], {})
        densities = [number(rows.get(cid, {}).get("density_person_per_m2")) for cid in cell_ids]
        # Missing cell rows cannot be silently treated as an empty area.
        if any(value is None for value in densities):
            densities = None
        return sample.get("density_person_per_m2"), densities

    if not state:
        # v1 has no evacuation lifecycle. Only the event baseline can be recovered.
        starts = []
        for command in _jsonl(directory / "commands.jsonl", issues):
            applied = command.get("result") or {}
            if applied.get("action") == "start" and applied.get("status") == "applied":
                value = number(applied.get("applied_at"))
                if value is not None:
                    starts.append(value)
        t0 = min(starts) if starts else None
    initial_global, initial = baseline("initial_density", t0)
    strategy_global, _ = baseline("strategy_density", ta)
    final_global, final = baseline("final_density", te)
    progress = state.get("progress") or {}
    final_sid = (state.get("final_density") or {}).get("snapshot_id")
    # Both commit evidence and normal-arrival accounting are necessary for a final value.
    final_ok = bool(te is not None and t0 is not None and te >= t0 and final_sid in committed
                    and final_sid in by_id and state.get("completion_evidence_committed") is True
                    and by_id[final_sid]["time_seconds"] == te
                    and state.get("status") == "complete" and number(progress.get("target_person_count"))
                    and number(progress.get("target_person_count")) == number(progress.get("normally_arrived_person_count"))
                    and all(number(progress.get(key)) == 0 for key in (
                        "remaining_person_count", "active_person_count", "pending_person_count",
                        "explicitly_removed_person_count", "unknown_disappearance_count",
                        "unexpected_person_count", "conservation_error")))
    if te is not None and not final_ok:
        issues["疏散结束缺少完整提交或正常到达证据（未展示终值）"] += 1
    if not final_ok:
        te, final_global, final = None, None, None
    duration = te - ta if te is not None and ta is not None and t0 <= ta <= te else None
    efficiency = ((strategy_global - final_global) / duration
                  if duration is not None and duration > 0 and strategy_global is not None and final_global is not None else None)
    metric_states = {}
    recorded_metrics = state.get("metrics") or result.get("metrics") or {}
    for name, value in (("evacuation-time", duration), ("evacuation-efficiency", efficiency)):
        raw = (recorded_metrics.get(name) or {}).get("status", "not_recorded")
        metric_states[name] = {"status": "complete" if value is not None else
                               "not_recorded" if source == "legacy" else
                               "zero_duration" if name == "evacuation-efficiency" and duration == 0 else
                               "incomplete" if raw == "complete" else raw, "value": value}
    metric_states["absolute-evacuation-density"] = {
        "status": "complete" if initial is not None and final is not None else
                  "partial_reconstructed" if initial is not None and source == "legacy" else
                  "partial" if initial is not None else "no_baseline", "value": None}
    progress_rows = {}
    for row in _jsonl(directory / "evacuation_progress.jsonl", issues):
        sid = row.get("snapshot_id")
        if sid in committed and number(row.get("last_time_seconds")) == committed[sid]["time_seconds"]:
            progress_rows[sid] = row.get("progress") or {}
    return {
        "source": source, "status": state.get("status", "not_recorded"),
        "t0": t0, "ta": ta, "te": te, "duration": duration, "efficiency": efficiency,
        "initial_global_density": initial_global, "strategy_global_density": strategy_global,
        "final_global_density": final_global, "initial": initial, "final": final,
        "progress": progress, "progress_series": [progress_rows.get(s["snapshot_id"]) for s in samples],
        "policies": state.get("policy_applications") or [], "metrics": metric_states,
        "basis": "本轮所有行人正常完成 SUMO 行程（简化口径）",
        "elapsed_since_strategy": max(0, samples[-1]["time_seconds"] - ta) if ta is not None and samples else None,
    }


def _roads(manifest, network_sha256, issues):
    """Only overlay current lane shapes when they match the run's frozen network."""
    config = Path(manifest.get("config_path") or "")
    if not config.is_file() or not network_sha256:
        return {"lines": [], "note": "仅显示实验保存的区域几何；原始路网不可用。"}
    try:
        net = ET.parse(config).find("./input/net-file")
        path = config.parent / net.attrib["value"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != network_sha256:
            return {"lines": [], "note": "当前路网已变更，使用实验保存的片区几何，未叠加当前道路。"}
        lines = []
        for lane in ET.parse(path).iter("lane"):
            if lane.get("allow") and "pedestrian" not in lane.get("allow").split():
                continue
            if "pedestrian" in lane.get("disallow", "").split():
                continue
            points = [[float(v) for v in point.split(",")[:2]] for point in lane.get("shape", "").split()]
            if len(points) >= 2:
                lines.append(points)
        return {"lines": lines, "note": "道路底图与该实验的路网哈希一致；坐标采用保存的 SUMO 米制坐标。"}
    except (OSError, ET.ParseError, KeyError, AttributeError, ValueError):
        issues["路网底图不可读取（仍使用保存的观测几何）"] += 1
        return {"lines": [], "note": "仅显示实验保存的片区几何。"}


def load_run(directory, max_map_frames=1200, scope_references=None):
    directory = Path(directory)
    issues = Counter()
    manifest = _json(directory / "manifest.json", issues, required=True)
    if manifest.get("run_id") not in {None, directory.name}:
        issues["manifest 的 run_id 与目录编号不同（以目录为准）"] += 1
    geometry = _json(directory / "observation_geometry.json", issues, required=True)
    result = _json(directory / "observation_result.json", issues)
    committed = {}
    for row in _jsonl(directory / "observation_samples.jsonl", issues):
        sid, time = row.get("snapshot_id"), number(row.get("time_seconds"))
        if not sid or time is None:
            issues["无效的快照提交索引"] += 1
            continue
        if sid in committed:
            issues["重复的快照提交编号（已合并）"] += 1
        committed[sid] = {**row, "time_seconds": time}
    ordered = sorted(committed.values(), key=lambda item: item["time_seconds"])
    if not ordered:
        raise ValueError("没有可读取的完整观测快照")
    for i, row in enumerate(ordered):
        row["first"] = i == 0
    globals_by_id = _rows(directory / "observation_global.csv", committed, issues)
    cells_by_id = _rows(directory / "observation_cells.csv", committed, issues, "cell_id")
    boundaries_by_id = _rows(directory / "observation_boundaries.csv", committed, issues, "boundary_id")
    cell_geo = [{"id": c["cell_id"], "area": number(c.get("area_m2")), "geometry": c["geometry"]}
                for c in geometry.get("cells", [])]
    boundary_geo = [{"id": b["boundary_id"], "a": b.get("cell_a"), "b": b.get("cell_b"),
                     "direction": b.get("direction_from_a"), "geometry": b["geometry"]}
                    for b in geometry.get("boundaries", [])]
    cell_ids = [c["id"] for c in cell_geo]
    boundary_ids = [b["id"] for b in boundary_geo]
    samples, densities, speeds, counts, differences = [], [], [], [], []
    for frame in ordered:
        sid = frame["snapshot_id"]
        global_row = globals_by_id.get(sid, {}).get("global")
        if global_row is None:
            issues["提交快照缺少全局数据（整帧已忽略）"] += 1
            continue
        samples.append({"snapshot_id": sid, "time_seconds": frame["time_seconds"],
                        **{field: number(global_row.get(field)) for field in GLOBAL_FIELDS}})
        cell_rows = cells_by_id.get(sid, {})
        boundary_rows = boundaries_by_id.get(sid, {})
        if len(cell_rows) != number(frame.get("cell_rows")):
            issues["片区行数与提交索引不一致的快照（保留空值）"] += 1
        if len(boundary_rows) != number(frame.get("boundary_rows")):
            issues["边界行数与提交索引不一致的快照（保留空值）"] += 1
        density = [number(cell_rows.get(cid, {}).get("density_person_per_m2")) for cid in cell_ids]
        densities.append(density)
        speeds.append([number(cell_rows.get(cid, {}).get("avg_speed_mps")) for cid in cell_ids])
        counts.append([number(cell_rows.get(cid, {}).get("person_count")) for cid in cell_ids])
        differences.append([number(boundary_rows.get(bid, {}).get("difference_person_per_m2"))
                            if boundary_rows.get(bid, {}).get("status") == "valid" else None for bid in boundary_ids])
    if not samples:
        raise ValueError("完整快照索引中没有匹配的全局数据")
    transitions, matrices = _transition_data(directory, committed, issues)
    evacuation = _evacuation(directory, result, committed, samples, cells_by_id, cell_ids, issues)
    first, final = evacuation.pop("initial"), evacuation.pop("final")
    initial_diff, final_diff, initial_final, mean_diff = [], [], [], []
    weights = [cell["area"] for cell in cell_geo]
    for sample, density in zip(samples, densities):
        valid = (sample["invalid_position_count"] == 0 and evacuation["t0"] is not None
                 and sample["time_seconds"] >= evacuation["t0"]
                 and (evacuation["te"] is None or sample["time_seconds"] <= evacuation["te"]))
        diff = lambda a, b: [abs(x - y) if valid and x is not None and y is not None else None
                            for x, y in zip(a or [None] * len(cell_ids), b or [None] * len(cell_ids))]
        values = [diff(first, density), diff(density, final), diff(first, final)]
        initial_diff.append(values[0]); final_diff.append(values[1]); initial_final.append(values[2])
        # Mean of absolute local differences, not absolute difference of global means.
        mean_diff.append([sum(value * area for value, area in zip(group, weights)) / sum(weights)
                          if weights and all(v is not None for v in group) and all(w and w > 0 for w in weights) else None
                          for group in values])
    required = [i for i, sample in enumerate(samples)
                if sample["time_seconds"] in (evacuation["t0"], evacuation["ta"], evacuation["te"])]
    map_indices = _sample_indices(len(samples), max(3, max_map_frames), required)
    maps = {"indices": map_indices, **{key: [rows[i] for i in map_indices] for key, rows in (
        ("density", densities), ("speed", speeds), ("count", counts), ("boundary", differences),
        ("initialRuntime", initial_diff), ("runtimeFinal", final_diff), ("initialFinal", initial_final))}}
    ranges = {name: max((abs(v) for row in rows for v in row if v is not None), default=0)
              for name, rows in (("density", densities), ("speed", speeds), ("boundary", differences),
                                 ("difference", initial_diff + final_diff + initial_final))}
    scenario = manifest.get("scenario") or {}
    requirement = manifest.get("requirement") or {}
    scope = requirement.get("spatial_scope") or {}
    scene_id = scenario.get("location_id") or scope.get("location_id") or "unknown"
    scope_info = _scope_info(geometry, scope, (scope_references or {}).get(scene_id))
    metrics = {name: dict((result.get("metrics") or {}).get(name) or {"status": "not_selected"})
               for _, name, _, _ in METRICS}
    metrics.update(evacuation["metrics"])
    requested = geometry.get("requested_metric_ids", geometry.get("metric_ids", []))
    for _, name, _, _ in METRICS:
        if name not in requested:
            metrics[name] = {"status": "not_selected"}
    roads = _roads(manifest, geometry.get("network_sha256"), issues)
    notes = []
    if scope_info["status"] == "changed":
        recorded_area = number(geometry.get("area_m2"))
        area_label = f"{recorded_area:.2f} m²" if recorded_area is not None else "面积未记录"
        notes.append(f"本轮采用不同范围 {scope_info['version']}（{area_label}），"
                     f"与已同步预设 {scope_info['reference']['version']} 不同。所有指标、面积和网格保留本轮原值；虚线仅用于边界对照。")
        if scope_info["overlay_geometry"] is None:
            notes.append("范围对照缺少兼容的投影/路网坐标证据，因此不叠加预设边界。")
    elif scope_info["status"] == "unavailable":
        notes.append("本轮缺少原始经纬度边界，无法与已同步预设比较；继续使用保存的观测区域。")
    if number(geometry.get("schema_version")) == 1:
        notes.append("旧版 v1 记录：没有疏散生命周期；B1/B2 不可还原，B3 仅在已记录首次运行时补算初始—运行时差。")
    if len(samples) == 1:
        notes.append("仅有一帧初始化数据，不能据此判断指标随时间的变化。")
    if len(map_indices) < len(samples):
        notes.append(f"空间图保留 {len(map_indices)} / {len(samples)} 帧；全局曲线与密度差均值保留全部帧，地图标明实际快照时间。")
    if any(s["invalid_position_count"] for s in samples):
        notes.append("部分帧含无效位置；A1/A2 仅反映可定位人员，B3 对应帧为空。")
    return _clean({
        "id": directory.name, "scene_id": scene_id,
        "scene_name": SCENES.get(scene_id, scope.get("name", scene_id)),
        "scope_name": scope.get("name", "未记录"), "requirement_id": requirement.get("requirement_id"),
        "hotspot": scenario.get("active_hotspot_name"), "source": str(directory.resolve()),
        "status": result.get("status", "unknown"), "end_reason": result.get("end_reason"),
        "error": result.get("error"), "schema_version": geometry.get("schema_version"),
        "area": number(geometry.get("area_m2")), "planned_count": (manifest.get("demand") or {}).get("planned"),
        "scope_info": scope_info,
        "network_sha256": geometry.get("network_sha256"), "notes": notes,
        "quality": [{"message": key, "count": count} for key, count in sorted(issues.items())],
        "geometry": {"scope": geometry.get("scope_geometry"), "cells": cell_geo, "boundaries": boundary_geo,
                     "grid_size": (geometry.get("parameters") or {}).get("grid_size_m"), "roads": roads},
        "samples": samples, "maps": maps, "ranges": ranges, "metrics": metrics, "evacuation": evacuation,
        "density_difference_means": mean_diff,
        "transitions": [dict(transitions[s["snapshot_id"]]) if transitions.get(s["snapshot_id"]) is not None else None for s in samples],
        "transition_matrices": matrices,
    })


def build_viewer(runs_root, output, run_ids=None, max_map_frames=1200, png=False, scope_reference_file=None):
    runs_root, output = Path(runs_root).resolve(), Path(output).resolve()
    if output == runs_root or runs_root in output.parents:
        raise ValueError("查看器输出目录必须位于 runs 之外，避免改写实验记录")
    if max_map_frames < 3:
        raise ValueError("max_map_frames 必须至少为 3")
    if output in runs_root.parents:
        raise ValueError("查看器输出不能是 runs 的父目录")
    if run_ids:
        directories = []
        for run_id in dict.fromkeys(run_ids):
            if Path(run_id).name != run_id or run_id in {".", ".."}:
                raise ValueError(f"无效 run_id: {run_id}")
            directory = (runs_root / run_id).resolve()
            if directory.parent != runs_root:
                raise ValueError(f"运行目录不属于指定 runs: {run_id}")
            directories.append(directory)
    else:
        directories = sorted(p.parent for p in runs_root.glob("*/observation_geometry.json"))
    scope_references = _load_scope_references(scope_reference_file)
    runs, skipped = [], []
    for directory in directories:
        try:
            runs.append(load_run(directory, max_map_frames, scope_references))
        except (OSError, ValueError, KeyError, TypeError, csv.Error) as error:
            skipped.append({"run_id": directory.name, "reason": str(error)})
    if not runs:
        raise ValueError("未找到可展示的观测记录。" + "; ".join(f"{s['run_id']}: {s['reason']}" for s in skipped))
    # Prefer runs matching the synced scope; within that scope prefer a process
    # over an initialization-only record. Historical data remains selectable.
    runs.sort(key=lambda run: (0 if run["scope_info"]["status"] == "matching" else 1,
                              -len(run["samples"]), run["id"]))
    data = {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "metrics": [{"code": c, "id": i, "name": n, "unit": u} for c, i, n, u in METRICS],
            "runs": runs, "skipped": skipped}
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    assets = Path(__file__).with_name("experiment_viewer_assets")
    template = (assets / "viewer.html").read_text(encoding="utf-8")
    # Metadata is data, not executable HTML (including names containing </script>).
    embedded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = template.replace("/* VIEWER_CSS */", (assets / "viewer.css").read_text(encoding="utf-8"))
    html = html.replace("/* VIEWER_JS */", (assets / "viewer.js").read_text(encoding="utf-8"))
    html = html.replace("__VIEWER_DATA__", embedded)
    output.mkdir(parents=True, exist_ok=True)
    (output / "data.json").write_text(encoded + "\n", encoding="utf-8")
    (output / "index.html").write_text(html, encoding="utf-8")
    if png:
        from crowdsim.infrastructure.experiment_viewer_plots import export_pngs
        export_pngs(data, output / "png")
    return data
