"""Strict post-analysis simulation and paired experiment request contracts."""

from __future__ import annotations

from .common import array, choice, fail, integer, number, object_, text_
from .metrics import METRICS

IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9_-]*"
MAX_SEED = 2147483647


def _metric_ids(raw, field: str) -> list[str]:
    values = array(raw, field, 1, len(METRICS))
    result = [choice(value, f"{field}[{index}]", set(METRICS)) for index, value in enumerate(values)]
    if len(set(result)) != len(result):
        fail(field, "指标 ID 不得重复")
    return result


def _scenario(data: dict) -> dict:
    raw = object_(data.get("scenario"), "scenario",
                  keys={"scenarioId", "modelId", "modelVersion", "startClock", "durationSeconds", "geometryVersion", "ruleVersion"})
    clock = choice(raw.get("startClock"), "scenario.startClock", {"20:00"})
    return {"scenarioId": text_(raw.get("scenarioId"), "scenario.scenarioId", max_len=64, pattern=IDENTIFIER),
            "modelId": text_(raw.get("modelId"), "scenario.modelId", max_len=64, pattern=IDENTIFIER),
            "modelVersion": text_(raw.get("modelVersion"), "scenario.modelVersion", max_len=80),
            "startClock": clock,
            "durationSeconds": integer(raw.get("durationSeconds"), "scenario.durationSeconds", 2, 86400),
            "geometryVersion": text_(raw.get("geometryVersion"), "scenario.geometryVersion", max_len=80),
            "ruleVersion": text_(raw.get("ruleVersion"), "scenario.ruleVersion", max_len=80)}


def validate_simulation(payload: dict) -> dict:
    data = object_(payload, "body", keys={"datasetId", "scenario", "seed", "metricIds"})
    return {"datasetId": text_(data.get("datasetId"), "datasetId", max_len=80, pattern=IDENTIFIER),
            "scenario": _scenario(data), "seed": integer(data.get("seed"), "seed", 0, MAX_SEED),
            "metricIds": _metric_ids(data.get("metricIds"), "metricIds")}


def validate_experiment(payload: dict) -> dict:
    data = object_(payload, "body", keys={"name", "datasetId", "scenario", "seedStart", "parallelism", "groups", "metricIds", "pcFeatures"})
    scenario = _scenario(data)
    raw_features = array(data.get("pcFeatures"), "pcFeatures", 2, 20)
    features = []
    feature_ids = set()
    for index, raw_feature in enumerate(raw_features):
        field = f"pcFeatures[{index}]"
        feature = object_(raw_feature, field,
                          keys={"id", "metricId", "regionId", "windowStartSeconds", "windowEndSeconds", "aggregation"})
        feature_id = text_(feature.get("id"), f"{field}.id", max_len=64, pattern=IDENTIFIER)
        if feature_id in feature_ids:
            fail(f"{field}.id", "PC 特征 ID 不得重复")
        feature_ids.add(feature_id)
        start = integer(feature.get("windowStartSeconds"), f"{field}.windowStartSeconds", 0, scenario["durationSeconds"] - 1)
        end = integer(feature.get("windowEndSeconds"), f"{field}.windowEndSeconds", start + 1, scenario["durationSeconds"])
        features.append({"id": feature_id,
                         "metricId": choice(feature.get("metricId"), f"{field}.metricId", set(METRICS)),
                         "regionId": text_(feature.get("regionId"), f"{field}.regionId", max_len=64, pattern=IDENTIFIER),
                         "windowStartSeconds": start, "windowEndSeconds": end,
                         "aggregation": choice(feature.get("aggregation"), f"{field}.aggregation", {"peak", "mean", "end"})})
    groups = array(data.get("groups"), "groups", 1, 12)
    result = []
    used_ids = set()
    for index, raw in enumerate(groups):
        field = f"groups[{index}]"
        group = object_(raw, field, keys={"id", "name", "repeatCount", "hypothesis", "manipulatedFeatureId", "interventions"})
        group_id = text_(group.get("id"), f"{field}.id", max_len=32, pattern=r"CF-[0-9]{2}")
        if group_id in used_ids:
            fail(f"{field}.id", "实验组 ID 不得重复")
        used_ids.add(group_id)
        interventions = array(group.get("interventions"), f"{field}.interventions", 1, 10)
        actions = []
        used_action_ids = set()
        for action_index, raw_action in enumerate(interventions):
            action_field = f"{field}.interventions[{action_index}]"
            action = object_(raw_action, action_field,
                             keys={"id", "actionType", "targetRegionId", "atSeconds", "parameters"})
            action_id = text_(action.get("id"), f"{action_field}.id", max_len=64, pattern=IDENTIFIER)
            if action_id in used_action_ids:
                fail(f"{action_field}.id", "同组干预 ID 不得重复")
            used_action_ids.add(action_id)
            at = integer(action.get("atSeconds"), f"{action_field}.atSeconds", 1, scenario["durationSeconds"] - 1)
            params = object_(action.get("parameters"), f"{action_field}.parameters")
            if len(params) > 20:
                fail(f"{action_field}.parameters", "参数项最多 20 个")
            for key, value in params.items():
                text_(key, f"{action_field}.parameters.key", max_len=64, pattern=IDENTIFIER)
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    fail(f"{action_field}.parameters.{key}", "参数必须是字符串或有限数值")
                if isinstance(value, (int, float)):
                    number(value, f"{action_field}.parameters.{key}", -1e9, 1e9)
                elif len(value) > 200:
                    fail(f"{action_field}.parameters.{key}", "字符串最多 200 字符")
            actions.append({"id": action_id,
                            "actionType": text_(action.get("actionType"), f"{action_field}.actionType", max_len=64, pattern=IDENTIFIER),
                            "targetRegionId": text_(action.get("targetRegionId"), f"{action_field}.targetRegionId", max_len=64, pattern=IDENTIFIER),
                            "atSeconds": at, "parameters": params})
        if [item["atSeconds"] for item in actions] != sorted(item["atSeconds"] for item in actions):
            fail(f"{field}.interventions", "干预必须按执行时间升序排列", "TIME_ORDER_INVALID")
        result.append({"id": group_id,
                       "name": text_(group.get("name"), f"{field}.name", max_len=120),
                       "repeatCount": integer(group.get("repeatCount"), f"{field}.repeatCount", 1, 50),
                       "hypothesis": text_(group.get("hypothesis"), f"{field}.hypothesis", max_len=500),
                       "manipulatedFeatureId": choice(group.get("manipulatedFeatureId"), f"{field}.manipulatedFeatureId", feature_ids),
                       "interventions": actions})
    seed = integer(data.get("seedStart"), "seedStart", 0, MAX_SEED)
    if seed + max(group["repeatCount"] for group in result) - 1 > MAX_SEED:
        fail("seedStart", "派生种子超出 32 位整数范围", "SEED_RANGE_EXCEEDED")
    metric_ids = _metric_ids(data.get("metricIds"), "metricIds")
    if any(feature["metricId"] not in metric_ids for feature in features):
        fail("pcFeatures", "PC 特征引用的指标必须包含在 metricIds 中", "METRIC_CAPABILITY_MISMATCH")
    return {"name": text_(data.get("name"), "name", max_len=120),
            "datasetId": text_(data.get("datasetId"), "datasetId", max_len=80, pattern=IDENTIFIER),
            "scenario": scenario, "seedStart": seed,
            "parallelism": integer(data.get("parallelism"), "parallelism", 1, 8),
            "metricIds": metric_ids,
            "pcFeatures": features, "groups": result}
