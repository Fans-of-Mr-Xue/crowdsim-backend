"""Execution catalogue for the six mandatory strong constraints (规则模型强约束).

Every confirmed artificial society must carry all six strong constraints. This
catalogue only advertises implementation methods the current CrowdSim runtime
genuinely executes; reserved placeholder alternatives are never surfaced as
selectable, so the front-end cannot pick a method that compilation and the
runtime would reject.

Chain of trust:
``catalogue (this file) -> compiler.validate / compile -> ArtificialSocietySpec
.parse -> SimulationRuntime.configure_artificial_society``. A method label
accepted at one stage must be accepted at every later stage.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

#: The six mandatory strong constraints, in canonical order.
STRONG_RULES: list[dict[str, Any]] = [
    {
        "id": "population-allocation",
        "category": "初始化规则",
        "name": "画像边际分布分配",
        "summary": "按已确认画像的边际比例抽样分配个体，保证总人数、角色与属性分布一致。",
        "default_method": "画像边际分布抽样",
        "runtime_consumed": True,
        "methods": [
            {
                "id": "profile-marginal-sampling",
                "label": "画像边际分布抽样",
                "implemented": True,
                "runtime_component": "PopulationProfileSampler",
                "module_path": "crowdsim.domain.population_profiles",
                "description": "按确认画像的边际比例抽样并装配个体属性，由 PopulationProfileSampler.configure_agent_types 在运行时执行。",
            }
        ],
    },
    {
        "id": "sumo-motion",
        "category": "运动规则",
        "name": "SUMO striping 行人运动",
        "summary": "由 SUMO striping 步行模型推进个体位移，并原生处理拥挤减速与交互。",
        "default_method": "SUMO striping 行人运动",
        "runtime_consumed": True,
        "methods": [
            {
                "id": "sumo-striping-motion",
                "label": "SUMO striping 行人运动",
                "implemented": True,
                "runtime_component": "SumoAdapter",
                "module_path": "crowdsim.infrastructure.sumo_adapter",
                "description": "SUMO striping 步行模型驱动位移与原生拥挤交互（SumoAdapter，pedestrian_model=striping）。",
            }
        ],
    },
    {
        "id": "local-observation",
        "category": "观察规则",
        "name": "局部观察与环境状态",
        "summary": "运行时逐个体维护局部邻域观测与当前可用目标集合。",
        "default_method": "运行时局部邻域观测",
        "runtime_consumed": True,
        "methods": [
            {
                "id": "runtime-local-observation",
                "label": "运行时局部邻域观测",
                "implemented": True,
                "runtime_component": "SimulationRuntime.observations",
                "module_path": "crowdsim.core.simulation_runtime",
                "description": "决策周期内由运行时维护个体局部邻域观测与可用目标集合。",
            }
        ],
    },
    {
        "id": "decision-scheduling",
        "category": "决策规则",
        "name": "到期决策与计划执行",
        "summary": "按决策周期到期触发决策，并由计划执行器把路线动作落地。",
        "default_method": "周期到期决策与计划执行",
        "runtime_consumed": True,
        "methods": [
            {
                "id": "periodic-decision-scheduling",
                "label": "周期到期决策与计划执行",
                "implemented": True,
                "runtime_component": "DecisionScheduler + PlanExecutor",
                "module_path": "crowdsim.decision",
                "description": "DecisionScheduler 按 decision_period_seconds 到期触发，PlanExecutor 落地路线动作。",
            }
        ],
    },
    {
        "id": "visual-state",
        "category": "状态规则",
        "name": "五状态权威分类",
        "summary": "按密度与速度阈值把个体归入五档权威可视状态。",
        "default_method": "五状态权威视觉分类",
        "runtime_consumed": True,
        "methods": [
            {
                "id": "five-state-visual-classification",
                "label": "五状态权威视觉分类",
                "implemented": True,
                "runtime_component": "CrowdVisualPolicy / StateUpdater",
                "module_path": "crowdsim.domain.crowd_visual_state",
                "description": "CrowdVisualPolicy 阈值经 StateUpdater 在每帧完成五档状态分类。",
            }
        ],
    },
    {
        "id": "message-control",
        "category": "消息与控制",
        "name": "消息、政策与控制动作",
        "summary": "投递信息并把政策、干预动作绑定到有效控制时段。",
        "default_method": "信息投递与干预执行",
        "runtime_consumed": True,
        "methods": [
            {
                "id": "information-intervention-control",
                "label": "信息投递与干预执行",
                "implemented": True,
                "runtime_component": "InformationModel + InterventionExecutor",
                "module_path": "crowdsim.environment",
                "description": "InformationModel 投递信息，InterventionExecutor 在有效控制时段执行干预动作。",
            }
        ],
    },
]


def strong_rule_catalogue() -> list[dict[str, Any]]:
    """Return a deep copy of the six mandatory strong-constraint entries."""
    return deepcopy(STRONG_RULES)


def rule_entry(rule_id: str) -> dict[str, Any] | None:
    return next((item for item in STRONG_RULES if item["id"] == rule_id), None)


def default_rule_methods() -> dict[str, str]:
    """Map each strong-constraint id to its genuinely executable default method."""
    return {item["id"]: item["default_method"] for item in STRONG_RULES}


def implemented_rule_method(rule_id: str, method: Any) -> bool:
    """True when ``method`` is a selectable, runtime-executable method for ``rule_id``."""
    entry = rule_entry(str(rule_id))
    if entry is None or method is None:
        return False
    return any(
        candidate["label"] == str(method) and candidate["implemented"]
        for candidate in entry["methods"]
    )


def resolve_rule_method(rule_id: str, method: Any) -> dict[str, Any] | None:
    """Return the executable method record for ``rule_id``/``method`` (default when omitted)."""
    entry = rule_entry(str(rule_id))
    if entry is None:
        return None
    label = str(method) if method else entry["default_method"]
    for candidate in entry["methods"]:
        if candidate["label"] == label and candidate["implemented"]:
            return deepcopy(candidate)
    return None
