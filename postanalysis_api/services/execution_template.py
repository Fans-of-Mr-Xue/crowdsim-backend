"""Explicit editable engineering templates; natural language is never executed."""
import hashlib
import json
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.workers.memorial_scenario import MODEL_ID, MODEL_VERSION, RULE_VERSION, METHODS, geometry_version


def review_version(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def execution_readiness(dataset, plan=None):
    """A valid scientific draft may still need a scene-specific execution mapping."""
    metadata = dataset.get("metadata") or {}
    profile = metadata.get("executionProfile")
    profile = profile if isinstance(profile, dict) else {}
    missing = []
    if metadata.get("durationSeconds") != 19800:
        missing.append("当前纪念塔执行器使用19800秒工程时窗；需提供当前事中场景自身的执行适配，不能拉伸观测时间")
    if profile.get("scenarioId") != "memorial-post-test":
        missing.append("场景模型、路网与控制动作映射")
    if not metadata.get("sourceRecords"):
        missing.append("可重建源流量与人员初始化的工程输入；仅有观测曲线不足以重新运行仿真")
    if missing:
        if metadata.get("sourceBinding", {}).get("sourcePhase") == "during":
            return {"ready": False, "code": "SOURCE_RUN_INPUTS_NOT_LINKED",
                    "message": "基线来自已完成的SUMO运行；事后尚未关联该轮原运行输入，已有观测和假设均已保留。",
                    "missing": ["原运行manifest、场景/模型/路网与控制配置的关联", "原始人员初始化、需求文件和种子等可重跑输入", "事后对该运行场景的控制与测量适配"],
                    "nextAction": "定位原事中运行目录并关联其输入与配置，再生成同场景的反事实执行请求；不需要把观测时窗改成19800秒。"}
        return {"ready": False, "code": "SCENE_EXECUTION_MAPPING_REQUIRED", "message": "方案可继续设计和保存；当前基线尚未绑定可执行场景。",
                "missing": missing, "nextAction": "返回需求界定核对措施与假设，补齐当前场景的工程执行映射后再运行。"}
    if plan and plan.get("counterfactualDesign", {}).get("schemaVersion") == 2:
        try:
            from postanalysis_api.schemas.unified_design import validate_unified_execution
            validate_unified_execution(plan)
        except ApiError as exc:
            return {"ready": False, "code": exc.code, "message": exc.message, "missing": [exc.message],
                    "nextAction": "返回需求界定补齐执行参数或模型映射，并重新确认方案。"}
    return {"ready": True, "code": "READY", "message": "已绑定纪念塔工程执行模型，须核对确认后运行。", "missing": [], "nextAction": "核对并确认工程执行矩阵。"}


def execution_template(dataset, plan=None):
    duration = dataset.get("metadata", {}).get("durationSeconds")
    readiness = execution_readiness(dataset, plan)
    if not readiness["ready"]:
        raise ApiError(readiness["code"], readiness["message"] + "；" + "；".join(readiness["missing"]), 422)
    unified = bool(plan and plan.get("counterfactualDesign", {}).get("schemaVersion") == 2)
    if unified:
        from postanalysis_api.schemas.unified_design import validate_unified_execution
        from postanalysis_api.schemas.review_intake import validate_review_intake
        validate_review_intake(plan)
        validate_unified_execution(plan)
    source = {"counterfactual": plan["counterfactualDesign"]["groups"]} if unified else plan["experimentSchemeDesign"] if plan else {
        kind: [{"id": prefix + "-01", "name": label, "hypothesis": hypothesis}]
        for kind, prefix, label, hypothesis in (
            ("observation", "OBS", "提前开放与引导世界线", "提前开放 C 测试通道并引导可改变密度和风险暴露"),
            ("intervention", "INT", "启动时点单因子实验", "固定引导比例与延迟，改变启动时点将影响风险暴露"),
            ("mechanism", "MEC", "引导响应模块消融", "固定开放时点，取消引导遵从模块后风险变化将减弱"))}
    repeats = (plan or {}).get("counterfactualSettings", {}).get("repeatCount", 6)
    from postanalysis_api.schemas.execution import validate_execution_settings
    settings = validate_execution_settings(plan["executionSettings"]) if plan and "executionSettings" in plan else None
    groups = []
    for kind in source:
        for design in source[kind]:
            if plan and not unified and design.get("method") != METHODS[kind]:
                raise ApiError("ANALYSIS_METHOD_UNSUPPORTED", f"当前纪念塔执行器只支持 {METHODS[kind]}，不能静默替换已确认方法", 422)
            config = design.get("execution")
            if config:
                from postanalysis_api.schemas.execution import validate_group_execution
                validate_group_execution(config)
            starts = [config["startSeconds"]] if config else [13800, 14100, 14400] if kind == "intervention" else [13800]
            for start in starts:
                groups.append({"id": f"CF-{len(groups) + 1:02d}", "name": design["name"] + (f" · {start}s" if len(starts) > 1 else ""),
                    "sourceGroupId": design["id"], "experimentType": kind, "repeatCount": repeats,
                    "hypothesis": design["hypothesis"], "manipulatedFeatureId": design.get("manipulatedFeatureId") or (settings["pcFeatures"][0]["id"] if settings else "late_density"),
                    "referenceGroupId": config["referenceDesignId"] if config else "F-00" if kind == "observation" else "CF-01",
                    "interventions": [
                        {"id": "open-C", "actionType": "open_gate", "targetRegionId": "C", "atSeconds": start, "parameters": {}},
                        {"id": "guide-C", "actionType": "guidance", "targetRegionId": "C", "atSeconds": start,
                         "parameters": {"compliance": config["compliance"] if config else 0 if kind == "mechanism" else 0.7,
                                        "delaySeconds": config["delaySeconds"] if config else 0}}]})
    mapping = {}
    for group in groups:
        mapping.setdefault(group["sourceGroupId"], group["id"])
    for group in groups:
        group["referenceGroupId"] = mapping.get(group["referenceGroupId"], group["referenceGroupId"])
    if len(groups) > 12:
        raise ApiError("RESOURCE_LIMIT_EXCEEDED", "默认展开后超过12组，请精简设计或手动提供执行方案", 422)
    result = {"name": (plan or {}).get("label", "纪念塔三类反事实完整测试")[:120], "datasetId": dataset["id"],
            **({"reviewPlanVersion": review_version(plan)} if plan else {}),
            "scenario": {"scenarioId": "memorial-post-test", "modelId": MODEL_ID, "modelVersion": MODEL_VERSION,
                         "geometryVersion": geometry_version(), "ruleVersion": RULE_VERSION,
                         "startClock": "20:00", "durationSeconds": duration},
            "seedStart": 20261023, "parallelism": 1, "groups": groups,
            "metricIds": ["peak_density", "mean_speed", "peak_pressure_proxy", "risk_exposed_unique", "risk_exposure", "congestion_duration", "evacuation_time"],
            "pcFeatures": [{"id": "early_density", "metricId": "peak_density", "regionId": "global",
                            "windowStartSeconds": 12000, "windowEndSeconds": 13800, "aggregation": "peak"},
                           {"id": "late_density", "metricId": "peak_density", "regionId": "global",
                            "windowStartSeconds": 13800, "windowEndSeconds": 16200, "aggregation": "peak"},
                           {"id": "late_speed", "metricId": "mean_speed", "regionId": "global",
                            "windowStartSeconds": 16200, "windowEndSeconds": duration, "aggregation": "mean"}]}
    if settings:
        result.update(settings)
    from postanalysis_api.schemas.experiments import validate_experiment
    validate_experiment(result)
    return result
