"""Versioned post-only demand compiler over the existing memorial SUMO network.

A/B/C -> M1/M2/M3 is an explicitly authorized engineering test mapping.
Source entrance stocks are calibration targets, not exact reconstructed people.
"""
from __future__ import annotations

from collections import deque
from functools import lru_cache
import hashlib
import random
from pathlib import Path
import xml.etree.ElementTree as ET

from postanalysis_api.schemas.common import ApiError

ROOT = Path(__file__).resolve().parents[2]
NET_FILE = ROOT / "scenarios/shanghai_bund/bund.net.xml"
ENTRY_EDGES = {"A": "178411801#0", "B": "177931018#0", "C": "monument_m3"}
MODEL_ID = "memorial-post-striping"
MODEL_VERSION = "post-striping/1.0"
RULE_VERSION = "post-controls/1.0"
METHODS = {"observation": "配对蒙特卡洛模拟", "intervention": "一次一因子法（OFAT）", "mechanism": "机制消融实验"}
BASELINE_CONTROL = {"gateOpenSeconds": 14700, "guidanceStartSeconds": 14700,
                    "guidanceDelaySeconds": 1500, "compliance": 0.4}
ACTION_SCHEMAS = {
    "open_gate": {},
    "guidance": {"compliance": {"type": "number", "minimum": 0, "maximum": 1, "required": True},
                 "delaySeconds": {"type": "integer", "minimum": 0, "maximum": 86400, "required": True}},
}
METRIC_IDS = ["peak_density", "mean_speed", "peak_pressure_proxy", "risk_exposed_unique",
              "risk_exposure", "congestion_duration", "evacuation_time"]


@lru_cache(maxsize=1)
def network():
    import sumolib
    return sumolib.net.readNet(str(NET_FILE), withInternal=True)


@lru_cache(maxsize=1)
def geometry_version():
    return "memorial-test-map-v1-" + hashlib.sha256(NET_FILE.read_bytes()).hexdigest()[:16]


def control_schedule(actions, baseline=None):
    controls = dict(baseline or BASELINE_CONTROL)
    used = set()
    for action in actions:
        if action["actionType"] in used:
            raise ApiError("ACTION_SCHEDULE_UNSUPPORTED", "当前模型每组仅支持一次通道开放和一次引导启动，不能静默忽略多段控制", 422)
        used.add(action["actionType"])
        if action["targetRegionId"] != "C":
            raise ApiError("ACTION_UNSUPPORTED", "当前控制动作仅支持 C 测试通道", 422)
        if action["actionType"] == "open_gate":
            controls["gateOpenSeconds"] = action["atSeconds"]
        elif action["actionType"] == "guidance":
            controls.update(guidanceStartSeconds=action["atSeconds"],
                            guidanceDelaySeconds=action["parameters"]["delaySeconds"],
                            compliance=action["parameters"]["compliance"])
        else:
            raise ApiError("ACTION_UNSUPPORTED", "未登记的事后动作", 422)
    return controls


def source_cohorts(dataset):
    from postanalysis_api.services.population_execution import MODE, prepare_execution_dataset, population_cohorts
    dataset = prepare_execution_dataset(dataset)
    profile = dataset.get("metadata", {}).get("executionProfile")
    if isinstance(profile, dict) and profile.get('inputMode') == MODE:
        return population_cohorts(dataset)
    if profile is not None:
        if not isinstance(profile, dict) or profile.get("scenarioId") != "memorial-post-test" or profile.get("baselineControl") != BASELINE_CONTROL:
            raise ApiError("EXECUTION_PROFILE_MISMATCH", "文件声明的场景或基线控制与当前版本不一致，不能静默替换", 422)
        mapping = profile.get("regionMapping", {})
        if not isinstance(mapping, dict) or any(not isinstance(mapping.get(key), dict) or mapping[key].get("edgeId") != edge for key, edge in ENTRY_EDGES.items()):
            raise ApiError("EXECUTION_PROFILE_MISMATCH", "文件的 A/B/C 路网映射与当前能力不一致", 422)
    records = dataset.get("metadata", {}).get("sourceRecords", [])
    if not records:
        raise ApiError("EXECUTION_INPUT_MISSING", "需上传包含 sourceRecords 的规范仿真数据，不能由稀疏人数猜测进入/离开流量", 422)
    if not isinstance(records, list) or len(records) > 50000 or any(not isinstance(row, dict) for row in records):
        raise ApiError("EXECUTION_INPUT_INVALID", "sourceRecords 必须是有限数量的记录对象", 422)
    if any(row.get("regionId") not in ENTRY_EDGES or type(row.get("timeSeconds")) is not int for row in records):
        raise ApiError("EXECUTION_INPUT_INVALID", "进出记录必须包含 A/B/C 区域及整数秒数", 422)
    cohorts, previous, queues = [], {}, {key: deque() for key in ENTRY_EDGES}
    duration = dataset.get("metadata", {}).get("durationSeconds", 0)
    if type(duration) is not int or not 2 <= duration <= 86400:
        raise ApiError("EXECUTION_INPUT_INVALID", "数据集缺少有效原始时间窗", 422)
    seen = set()
    for row in sorted(records, key=lambda row: (row["timeSeconds"], row["regionId"])):
        region, end = row.get("regionId"), row.get("timeSeconds")
        if region not in queues or type(end) is not int or not 0 <= end <= duration or (region, end) in seen:
            raise ApiError("EXECUTION_INPUT_INVALID", "原始进出流量的区域或时间无效/重复", 422)
        seen.add((region, end))
        start = previous.get(region, 0)
        entered, exited = row.get("intervalEntered"), row.get("intervalExited")
        if any(type(value) is not int or not 0 <= value <= 100000 for value in (entered, exited)):
            raise ApiError("EXECUTION_INPUT_INVALID", "区间进入/离开必须为非负整数", 422)
        if len(cohorts) + entered > 20000:
            raise ApiError("RESOURCE_LIMIT_EXCEEDED", "单次事后执行最多支持两万人", 422)
        for index in range(entered):
            departure = start + (end - start) * (index + 0.5) / max(1, entered)
            person = {"id": f"p_{region}_{len(cohorts):06d}", "regionId": region,
                      "depart": departure, "release": float(duration + 1),
                      "agePercent": row.get("agePercent", {}), "nationalityPercent": row.get("nationalityPercent", {})}
            cohorts.append(person)
            queues[region].append(person)
        # FIFO is a declared reconstruction assumption; residuals remain visible.
        for index in range(exited):
            if queues[region]:
                person = queues[region].popleft()
                person["release"] = max(person["depart"], start + (end - start) * (index + 0.5) / max(1, exited))
        previous[region] = end
    return cohorts


@lru_cache(maxsize=128)
def _route(from_edge, to_edge):
    net = network()
    path, _ = net.getShortestPath(net.getEdge(from_edge), net.getEdge(to_edge),
                                  vClass="pedestrian", ignoreDirection=True)
    if not path:
        raise ApiError("ROUTE_UNREACHABLE", f"行人路径不可达：{from_edge} → {to_edge}", 422)
    return " ".join(edge.getID() for edge in path)


@lru_cache(maxsize=1)
def holding_segments():
    segments = []
    for edge in network().getEdges():
        if edge.getID().startswith("679361567"):
            for lane in edge.getLanes():
                if lane.allows("pedestrian") and lane.getLength() > 2:
                    segments.append((edge.getID(), lane.getID(), lane.getLength()))
                    break
    if not segments:
        raise ApiError("REGION_UNMAPPED", "纪念塔环道缺少可用停留区", 422)
    return sorted(segments)


def compile_routes(dataset, seed, actions, duration, output: Path):
    from postanalysis_api.services.population_execution import MODE, prepare_execution_dataset
    dataset = prepare_execution_dataset(dataset)
    profile = dataset.get('metadata', {}).get('executionProfile', {})
    reconstructed = profile.get('inputMode') == MODE
    controls = control_schedule(actions, profile.get('baselineControl'))
    cohorts = source_cohorts(dataset)
    rng = random.Random(seed)
    routes = ET.Element("routes")
    ET.SubElement(routes, "vType", id="post-ped", vClass="pedestrian", maxSpeed="1.34", speedDev="0.1")
    manifest = []
    initial_counts = {region: sum(person["depart"] == 0 and person["regionId"] == region for person in cohorts) for region in ENTRY_EDGES}
    initial_indices = {region: 0 for region in ENTRY_EDGES}
    for person in sorted(cohorts, key=lambda value: (value["depart"], value["id"])):
        if person["depart"] >= duration:
            continue
        region, release = person["regionId"], person["release"]
        # Common random number draw per person in every world/seed.
        follow = rng.random()
        segments = holding_segments()
        distance = rng.random() * sum(length - 2 for _, _, length in segments)
        hold_edge, hold_lane, hold_length = segments[-1]
        for candidate_edge, candidate_lane, length in segments:
            if distance <= length - 2:
                hold_edge, hold_lane, hold_length = candidate_edge, candidate_lane, length
                break
            distance -= length - 2
        hold_position = min(hold_length - 1, 1 + distance)
        entry_length = network().getEdge(ENTRY_EDGES[region]).getLength()
        if person["depart"] == 0:
            entry_position = 0.2 + (initial_indices[region] + 0.5) / max(1, initial_counts[region]) * max(0, entry_length - 0.4)
            initial_indices[region] += 1
        else:
            entry_position = 0.2 + rng.random() * max(0, entry_length - 0.4)
        destination = region
        guidance_effective = controls["guidanceStartSeconds"] + controls["guidanceDelaySeconds"]
        if region in {"A", "B"} and release >= guidance_effective and follow < controls["compliance"]:
            destination = "C"
            if reconstructed:
                release = max(person['depart'] + 1, min(release, guidance_effective))
        if destination == "C":
            release = max(release, controls["gateOpenSeconds"])
        node = ET.SubElement(routes, "person", id=person["id"], type="post-ped", depart=f"{person['depart']:.3f}",
                             departPos=f"{entry_position:.3f}", departPosLat="random")
        ET.SubElement(node, "walk", edges=_route(ENTRY_EDGES[region], hold_edge), arrivalPos=f"{hold_position:.3f}")
        ET.SubElement(node, "stop", lane=hold_lane, endPos=f"{hold_position:.3f}", posLat="0", until=f"{max(person['depart'] + 1, release):.3f}")
        ET.SubElement(node, "walk", edges=_route(hold_edge, ENTRY_EDGES[destination]), arrivalPos="1")
        manifest.append({**person, "exitRegionId": destination, "release": release,
                         "holdingEdge": hold_edge, "holdingPosition": hold_position, "entryPosition": entry_position})
    output.write_bytes(ET.tostring(routes, encoding="utf-8", xml_declaration=True))
    return {"controls": controls, "cohortCount": len(manifest), "cohorts": manifest,
            "reconstruction": dataset.get('metadata', {}).get('reconstruction'),
            "assumptions": ["A→M1、B→M2、C→M3 为已确认测试映射；M3 不是真实北侧通道",
                            "区间进入均匀分配、区间离开按 FIFO 重建停留时长，非原始个人轨迹",
                            "停留位置按共同随机数分散在纪念塔既有环道，避免单点排队伪影",
                            "初始存量沿入口分层分布，后续区间进入沿入口随机注入；不在同一点叠放全部人员",
                            "年龄/国籍只保留原始边际分布，不据此推断行为差异",
                            "SUMO striping 不模拟跌倒、伤亡或机械接触压力"]}
