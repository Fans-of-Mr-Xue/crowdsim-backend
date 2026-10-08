"""Versioned metric definitions and four density-based risk levels."""

METRIC_VERSION = "post-metrics/1.0"
RISK_RULE_VERSION = "rule-model/1.0"

METRICS = {
    "peak_density": {"unit": "person/m2", "direction": "down", "requires": ["grid_population", "effective_area"],
                     "definition": "所有固定网格及时间点的最大人口数/有效面积"},
    "mean_speed": {"unit": "m/s", "direction": "up", "requires": ["person_velocity", "dt"],
                   "definition": "全运行人员-时间加权的速度模长均值，包含零速人员"},
    "peak_pressure_proxy": {"unit": "person/m2*(m/s)^2", "direction": "down",
                            "requires": ["person_position", "person_velocity", "kernel_radius"],
                            "definition": "固定观测点上密度乘速度向量加权方差的时空最大值；不是机械接触力"},
    "risk_exposed_unique": {"unit": "person", "direction": "down", "requires": ["person_id", "person_position", "risk_grid"],
                            "definition": "曾进入所选密度风险等级网格的去重人数"},
    "risk_exposure": {"unit": "person*s", "direction": "down", "requires": ["person_id", "person_position", "risk_grid", "dt"],
                      "definition": "每个时间步在所选风险等级网格内的人数乘仿真步长后求和"},
    "congestion_duration": {"unit": "s", "direction": "down", "requires": ["grid_density", "grid_mean_speed", "dt"],
                            "definition": "同一网格密度>=3.5且平均速度<=0.5，连续至少10秒的拥挤事件累计时长"},
    "evacuation_time": {"unit": "s", "direction": "down", "requires": ["target_person_ids", "safe_zone_ids", "arrival_times"],
                        "definition": "目标人员全部到达安全区的时间减疏散起点；未完成时右删失"},
    "resource_cost": {"unit": "cost_unit", "direction": "down", "requires": ["resource_usage", "versioned_cost_table"],
                      "definition": "按已登记成本表对资源使用量逐项计价后求和"},
}

RISK_LEVELS = [
    {"id": "low", "label": "低", "minDensity": None, "maxDensityExclusive": 1.0},
    {"id": "medium", "label": "中", "minDensity": 1.0, "maxDensityExclusive": 2.0},
    {"id": "medium_high", "label": "中高", "minDensity": 2.0, "maxDensityExclusive": 3.5},
    {"id": "high", "label": "高", "minDensity": 3.5, "maxDensityExclusive": None},
]

def catalog() -> dict:
    return {"metricDefinitionVersion": METRIC_VERSION, "riskRuleVersion": RISK_RULE_VERSION,
            "gridSizeMeters": 5, "slidingWindowSeconds": 10,
            "gatheringEvent": {"densityAtLeast": 2.0, "durationSeconds": 10},
            "congestionEvent": {"densityAtLeast": 3.5, "meanSpeedAtMost": 0.5, "durationSeconds": 10},
            "policyTriggers": {
                "broadcast": {"gatheringDurationAtLeastSeconds": 30,
                              "releaseDensityBelow": 1.5, "releaseDurationSeconds": 30},
                "diversion": {"congestionDurationAtLeastSeconds": 10,
                              "releaseDensityBelow": 2.5, "releaseMeanSpeedAbove": 0.7},
                "alternate_route": {"outflowInflowRatioBelow": 0.7,
                                    "releaseRatioAtLeast": 0.9, "releaseDurationSeconds": 20}},
            "levels": RISK_LEVELS, "metrics": METRICS}
