"""Post-review v2: one experiment pool, independent of downstream analysis pages."""
from .common import ApiError
from .execution import validate_group_execution
from .metrics import METRICS

CONTROL_FIELDS = {"startSeconds": "T_start", "compliance": "Compliance", "delaySeconds": "Delay"}
BASELINE = {"startSeconds": 14700, "compliance": .4, "delaySeconds": 1500}
METHODS = {"paired", "ofat", "ablation"}


def validate_unified_design(design, variables):
    def fail(message):
        raise ApiError("POST_REVIEW_INCOMPLETE", message, 422, "plan.counterfactualDesign")
    if not isinstance(design, dict) or design.get("schemaVersion") != 2:
        fail("统一实验设计版本无效")
    for key in ("controlledVariables", "unit", "scope"):
        if not isinstance(design.get(key), str) or not design[key].strip():
            fail("请明确共同控制条件、实验单位和分析范围")
    groups = design.get("groups")
    if not isinstance(groups, list) or not 1 <= len(groups) <= 12:
        fail("统一实验池需要1—12个组")
    import re
    ids = set()
    for group in groups:
        allowed = {"id", "name", "hypothesis", "method", "factorSymbols", "primaryMetricId", "expectedDirection", "decisionRule", "execution"}
        if isinstance(group, dict) and set(group) - allowed:
            fail("实验组包含未定义字段")
        if not isinstance(group, dict) or any(not isinstance(group.get(key), str) or not group[key].strip() or len(group[key]) > limit
            for key, limit in (("id", 32), ("name", 120), ("hypothesis", 500), ("decisionRule", 2000))):
            fail("每组须明确编号、名称、可检验假设和判定规则")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", group["id"]) or group["id"] == "F-00" or group["id"] in ids:
            fail("实验组编号无效或重复")
        ids.add(group["id"])
        if not isinstance(group.get("method"), str) or group["method"] not in METHODS or not isinstance(group.get("expectedDirection"), str) or group["expectedDirection"] not in {"increase", "decrease", "two_sided"}:
            fail("实验方法或预期方向无效")
        responses = variables.get("responses", [])
        if not isinstance(group.get("primaryMetricId"), str) or not any(group["primaryMetricId"] == (item.get("id") or item.get("symbol")) for item in responses):
            fail("主要响应指标必须已经定义")
        factors = group.get("factorSymbols")
        defined = {item.get("symbol") for item in variables.get("factors", [])}
        if not isinstance(factors, list) or not factors or any(not isinstance(symbol, str) or symbol not in defined for symbol in factors):
            fail("每组须关联已定义的变量")
        validate_group_execution(group.get("execution"))
    for group in groups:
        reference = group["execution"]["referenceDesignId"]
        if reference != "F-00" and reference not in ids or reference == group["id"]:
            fail("参照组不存在或引用自身")
        seen = {group["id"]}
        while reference != "F-00":
            if reference in seen:
                fail("参照组不得循环引用")
            seen.add(reference)
            reference = next(item for item in groups if item["id"] == reference)["execution"]["referenceDesignId"]
        changes = changed_controls(group, groups)
        if any(CONTROL_FIELDS[field] not in group["factorSymbols"] for field in changes):
            fail("实际改变的变量须全部关联到假设")
        if group["method"] == "ofat" and len(changes) > 1:
            fail("单因素对照只能改变一个变量")
        if group["method"] == "ablation" and (group["execution"]["referenceDesignId"] == "F-00" or len(changes) != 1 or changes[0] == "startSeconds"):
            fail("消融须参照完整策略组且只改变遵从或延迟模块")
    return design


def changed_controls(group, groups):
    reference = next((item["execution"] for item in groups if item["id"] == group["execution"]["referenceDesignId"]), BASELINE)
    return [key for key in CONTROL_FIELDS if reference[key] != group["execution"][key]]


def validate_unified_execution(plan):
    """Do not silently ignore custom society changes or unimplemented endpoints."""
    overrides = plan.get("artificialSociety", {}).get("overrides", {})
    if any(overrides.values()):
        raise ApiError("SOCIETY_MAPPING_REQUIRED", "人工社会修改已保存；当前执行器尚未映射这些模型改动，需对接后运行或明确恢复原配置", 422)
    for group in plan["counterfactualDesign"]["groups"]:
        if group["primaryMetricId"] not in METRICS or set(group["factorSymbols"]) - set(CONTROL_FIELDS.values()):
            raise ApiError("EXECUTION_MAPPING_REQUIRED", "自定义变量或主要终点尚未接入当前执行模型", 422)
        if group["primaryMetricId"] not in plan.get("executionSettings", {}).get("metricIds", []):
            raise ApiError("EXECUTION_MAPPING_REQUIRED", "主要响应指标必须包含在实际输出指标中", 422)
