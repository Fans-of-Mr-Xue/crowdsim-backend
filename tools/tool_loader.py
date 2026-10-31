"""按 Agent 声明的名称装载事后工具，并接入已有事后服务契约。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable

from .tool_registry import Tool, ToolRegistry


@dataclass
class PostToolContext:
    """由调用方提供身份、工作区和已初始化的 PostAnalysisApp。"""

    app: Any
    user: str
    workspace: str
    services: dict[str, Callable[[dict], Any]] | None = None
    confirmed_goal: dict | None = None
    skill_registry: Any = None
    mode: str | None = None
    phase: str | None = "post"

    def require(self, name: str) -> Callable[[dict], Any]:
        service = (self.services or {}).get(name)
        if service is None:
            raise LookupError(f"{name} 尚未接入运行服务")
        return service


def _schema(required: dict[str, dict] | None = None, optional: dict[str, dict] | None = None) -> dict:
    properties = {**(required or {}), **(optional or {})}
    return {"type": "object", "properties": properties, "required": list(required or {}),
            "additionalProperties": False}


_TEXT = {"type": "string"}
_OBJECT = {"type": "object"}
_ARRAY = {"type": "array", "items": {"type": "object"}}
_BOOL = {"type": "boolean"}
_INTEGER = {"type": "integer", "minimum": 0}

# 名称严格对照 Ask.agent.md 与 PostAgent.agent.md
_SPECS: dict[str, tuple[str, dict, str]] = {
    "judge_stage": ("判断对话应进入事前、事中还是事后，并给出理由。", _schema({"message": _TEXT}), "对话"),
    "intrudec_platform": ("介绍 CrowdSim 平台功能。", _schema(), "对话"),
    "explain_function_of_stage": ("解释指定阶段的功能。", _schema({"stage": _TEXT}), "对话"),
    "get_user_goal": ("读取用户已确认的事后分析目标和子问题。", _schema(), "对话"),
    "register_upload_dataset": ("将已解析且标准化的时间线及可选原件注册为版本化数据集。", _schema({"dataset": _OBJECT}), "数据"),
    "get_event_snapshot_by_id": ("按 ID 获取事中传来的事件快照。", _schema({"eventId": _TEXT}), "数据"),
    "inspect_dataset": ("读取数据集元信息和分页时间线，不推断缺失字段。", _schema({"datasetId": _TEXT}, {"offset": _INTEGER, "limit": _INTEGER}), "数据"),
    "validate_dataset": ("按事后数据集契约校验标准化时间线。", _schema({"dataset": _OBJECT}), "数据"),
    "serch_case_library": ("检索事前案例库，返回可追溯案例。", _schema({"query": _TEXT}), "知识"),
    "query_causal_storage": ("查询既有因果存储，返回来源与证据等级。", _schema({"query": _TEXT}), "知识"),
    "check_causal_readiness": ("检查 PC 输入是否为足量独立样本及完整数值列。", _schema({"samples": _ARRAY, "independentUnitsConfirmed": _BOOL}), "因果"),
    "run_pc_analysis": ("对通过适用性检查的独立样本运行 Gaussian PC，结果仅作候选结构。", _schema({"samples": _ARRAY, "independentUnitsConfirmed": _BOOL}), "因果"),
    "get_simulation_context": ("读取场景、模型、区域、动作、指标与运行器能力声明。", _schema(), "仿真"),
    "validate_experiment_design": ("按事后批次请求结构验证完整 F-00/CF 方案。", _schema({"design": _OBJECT}), "设计"),
    "compile_sumo_config": ("将已验证方案编译成真实 SUMO 执行配置。", _schema({"design": _OBJECT}), "仿真"),
    "submit_simulation_task": ("经用户确认后创建或启动事后实验批次；无执行器时保留草稿。", _schema({"userConfirmed": _BOOL}, {"experimentId": _TEXT, "design": _OBJECT}), "仿真"),
    "get_simulation_progress": ("读取实验真实状态和游标事件。", _schema({"experimentId": _TEXT}, {"after": _INTEGER}), "仿真"),
    "get_simulation_results": ("读取已封存实验的指标、对照和候选因果图。", _schema({"experimentId": _TEXT}), "分析"),
    "analyze_simulation_results": ("读取事后观察、干预、机制三段结构化结果。", _schema({"experimentId": _TEXT}), "分析"),
    "generate_observe_answer": ("基于观察结果回答用户子问题并引用证据。", _schema({"experimentId": _TEXT, "question": _TEXT}), "分析"),
    "generate_intervene_answer": ("基于干预结果回答用户子问题并引用证据。", _schema({"experimentId": _TEXT, "question": _TEXT}), "分析"),
    "generate_mechanism_answer": ("基于机制事件回答用户子问题并引用证据。", _schema({"experimentId": _TEXT, "question": _TEXT}), "分析"),
    "generate_causal_report": ("汇总三段分析，生成可追溯事后报告。", _schema({"experimentId": _TEXT}), "报告"),
}


def _read_result(context: PostToolContext, experiment_id: str, sections: tuple[str, ...]) -> dict:
    return {section: context.app.result(experiment_id, context.user, context.workspace, section)
            for section in sections}


def _execute(name: str, context: PostToolContext, params: dict) -> Any:
    app = context.app
    user, workspace = context.user, context.workspace
    if name == "judge_stage":
        message = params["message"]
        matches = {"post": ("复盘", "反事实", "因果", "事后", "报告", "分析", "实验", "总结", "经验", "教训", "历史"),
                   "during": ("事中", "实时", "正在", "监控"),
                   "pre": ("事前", "预案", "筹备", "预测")}
        stages = [stage for stage, words in matches.items() if any(word in message for word in words)]
        return {"stage": stages[0] if len(stages) == 1 else "unclear",
                "candidates": stages, "requiresUserConfirmation": True}
    if name == "intrudec_platform":
        return {"platform": "CrowdSim", "stages": ["事前研判", "事中决策", "事后复盘解释"]}
    if name == "explain_function_of_stage":
        descriptions = {"pre": "收集情报和规划方案", "during": "监控实验过程并辅助决策",
                        "post": "接入数据、设计反事实实验、分析结果并生成复盘报告"}
        stage = params["stage"]
        return {"stage": stage, "description": descriptions.get(stage, "请指定 pre、during 或 post")}
    if name == "get_user_goal":
        if not context.confirmed_goal:
            return {"status": "needs_user_confirmation", "missing": ["analysis_goal", "subquestions"]}
        return {"status": "confirmed", "goal": context.confirmed_goal}
    if name == "register_upload_dataset":
        return app.dataset_metadata(app.create_dataset(user, workspace, params["dataset"]))
    if name == "validate_dataset":
        from postanalysis_api.schemas.datasets import validate_dataset
        normalized = validate_dataset(params["dataset"])
        return {"valid": True, "observationCount": len(normalized["observations"]),
                "sourceKind": normalized["sourceKind"],
                "firstTimeSeconds": normalized["observations"][0]["timeSeconds"],
                "lastTimeSeconds": normalized["observations"][-1]["timeSeconds"]}
    if name == "inspect_dataset":
        dataset_id = params["datasetId"]
        dataset = app.repo.get("datasets", dataset_id, user, workspace)
        offset, limit = params.get("offset", 0), params.get("limit", 100)
        if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("offset 必须非负，limit 必须在 1 至 1000 之间")
        return {"metadata": app.dataset_metadata(dataset),
                "observations": dataset["observations"][offset:offset + limit],
                "nextOffset": offset + limit if offset + limit < len(dataset["observations"]) else None}
    if name in {"check_causal_readiness", "run_pc_analysis"}:
        samples = params["samples"]
        if not isinstance(samples, list) or not all(isinstance(row, dict) for row in samples):
            raise ValueError("samples 必须是独立样本对象数组")
        keys = list(samples[0]) if samples else []
        minimum = max(5, len(keys) + 4)
        complete = all(
            set(row) == set(keys) and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                     and math.isfinite(value) for value in row.values()) for row in samples)
        varying = bool(samples) and all(
            max(row[key] for row in samples) - min(row[key] for row in samples) > 1e-12
            for key in keys) if complete else False
        valid = params["independentUnitsConfirmed"] is True and len(samples) >= minimum and len(keys) >= 2 and complete and varying
        if name == "check_causal_readiness" or not valid:
            return {"ready": valid, "sampleCount": len(samples), "requiredMinimum": minimum,
                    "featureIds": keys, "reason": None if valid else "INDEPENDENT_NUMERIC_SAMPLES_REQUIRED"}
        from algorithms.pc import pc_discovery
        try:
            graph = pc_discovery(samples)
        except ValueError as exc:
            return {"status": "insufficient_data", "sampleCount": len(samples),
                    "reason": "PC_ASSUMPTIONS_OR_VARIANCE_FAILED", "detail": str(exc)}
        return {"status": "candidate_only", "sampleCount": len(samples),
                "algorithm": "Gaussian-PC-stable", "graph": graph}
    if name == "get_simulation_context":
        return app.capabilities()
    if name == "validate_experiment_design":
        from postanalysis_api.schemas.experiments import validate_experiment
        design = validate_experiment(params["design"])
        capability = app.capabilities()["simulation"]
        if capability.get("ready"):
            app._check_start_capability(design)
        return {"valid": True, "design": design, "executable": bool(capability.get("ready")),
                "blockingReason": None if capability.get("ready") else capability.get("reason", "EXECUTOR_UNAVAILABLE")}
    if name == "submit_simulation_task":
        if params["userConfirmed"] is not True:
            raise ValueError("启动真实仿真前需要用户明确确认方案")
        if bool(params.get("design")) == bool(params.get("experimentId")):
            raise ValueError("必须且只能提供 design 或 experimentId")
        task = (app.create_experiment(user, workspace, params["design"])
                if "design" in params else app.repo.get("experiments", params["experimentId"], user, workspace))
        simulation = app.capabilities()["simulation"]
        if not simulation.get("ready"):
            return {"task": task, "started": False,
                    "reason": simulation.get("reason", "EXECUTOR_UNAVAILABLE")}
        return {"task": app.start("experiments", task["id"], user, workspace), "started": True}
    if name == "get_simulation_progress":
        experiment_id = params["experimentId"]
        return {"task": app.repo.get("experiments", experiment_id, user, workspace),
                "events": app.repo.events("experiments", experiment_id, user, workspace, params.get("after", 0))}
    if name == "get_simulation_results":
        return _read_result(context, params["experimentId"], ("metrics", "comparison", "causalGraph"))
    if name == "analyze_simulation_results":
        experiment_id = params["experimentId"]
        available = _read_result(context, experiment_id, ("observation", "intervention"))
        try:
            available["mechanism"] = app.result(experiment_id, user, workspace, "mechanism")
        except Exception as exc:
            if getattr(exc, "code", None) != "ANALYSIS_UNAVAILABLE":
                raise
            available["mechanism"] = {"status": "unavailable", "reason": "MECHANISM_EVIDENCE_MISSING"}
        return available
    # 外部案例库、事中快照、LLM 文案与 SUMO 编译器由调用方按同名服务注入。
    return context.require(name)(params)


class NamedTool(Tool):
    def __init__(self, name: str):
        self.name = name
        self.description, self.input_schema, self.category = _SPECS[name]

    def execute(self, context: PostToolContext, params: dict) -> str:
        try:
            result = _execute(self.name, context, params)
            return json.dumps({"success": True, "data": result}, ensure_ascii=False, allow_nan=False)
        except ValueError as exc:
            return json.dumps({"success": False, "error": {"code": "VALIDATION_ERROR", "message": str(exc)}},
                              ensure_ascii=False)
        except LookupError as exc:
            return json.dumps({"success": False, "error": {"code": "TOOL_UNAVAILABLE", "message": str(exc)}},
                              ensure_ascii=False)
        except Exception as exc:
            from postanalysis_api.schemas.common import ApiError
            if not isinstance(exc, ApiError):
                raise
            return json.dumps({"success": False, "error": {"code": exc.code, "message": exc.message,
                                                           "field": exc.field}}, ensure_ascii=False)


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in _SPECS:
        registry.register(NamedTool(name))
    return registry


def create_agent_registry(agent_definition) -> ToolRegistry:
    """仅暴露 Agent frontmatter 明确声明的工具。"""
    full = create_default_registry()
    registry = ToolRegistry()
    for name in agent_definition.tool_allowlist:
        tool = full.get(name)
        if tool is None:
            raise ValueError(f"Agent 声明的工具未注册: {name}")
        registry.register(tool)
    if agent_definition.skills:
        from .skill_tool import SkillTool
        registry.register(SkillTool())
    return registry
