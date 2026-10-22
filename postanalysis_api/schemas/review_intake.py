"""Validate the confirmed post-review design snapshot, independently of execution."""
from .common import ApiError


def validate_review_intake(plan):
    intake = plan.get("reviewIntake")
    if intake is None:  # Existing plan import contracts remain supported.
        return
    def fail(message):
        raise ApiError("POST_REVIEW_INCOMPLETE", message, 422, "plan.reviewIntake")
    if not isinstance(intake, dict) or intake.get("schemaVersion") != 1:
        fail("事后需求版本无效")
    baseline = plan.get("factualBaseline") or {}
    if not isinstance(baseline, dict):
        fail("事实基线必须是对象")
    if intake.get("baselineConfirmed") is not True or baseline.get("reconstructionConfirmed") is not True:
        fail("请先明确确认事实基线")
    if intake.get("goalConfirmed") is not True or not isinstance(plan.get("userIntent"), str) or not plan["userIntent"].strip():
        fail("请先确认规范化后的实验目的")
    stages = intake.get("confirmedStages")
    if not isinstance(stages, dict) or any(stages.get(kind) is not True for kind in ("observation", "intervention", "mechanism")):
        fail("请依次确认观察、干预和机制实验设计")
    groups = plan.get("experimentSchemeDesign")
    if not isinstance(groups, dict):
        fail("缺少三类实验设计")
    total, ids = 0, set()
    for kind in ("observation", "intervention", "mechanism"):
        rows = groups.get(kind)
        if not isinstance(rows, list) or not rows:
            fail(f"{kind} 至少需要一个实验组")
        for row in rows:
            if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key].strip()
                                               for key in ("id", "name", "hypothesis", "factor", "change", "method")):
                fail("每个实验组必须明确名称、假设、因素、变化与方法")
            if row["id"] in ids:
                fail("实验组 ID 不能重复")
            ids.add(row["id"])
        total += len(rows)
    if not 3 <= total <= 12:
        fail("三类实验合计需要 3–12 个组")
    settings = plan.get("counterfactualSettings")
    if not isinstance(settings, dict):
        fail("缺少重复实验配置")
    repeats = settings.get("repeatCount")
    if type(repeats) is not int or not 1 <= repeats <= 50:
        fail("重复次数须为 1–50 的整数")
    variables = plan.get("variableDefinitions") or {}
    if not isinstance(variables, dict):
        fail("变量定义必须是对象")
    for kind in ("factors", "responses"):
        definitions = variables.get(kind)
        if not isinstance(definitions, list) or not definitions:
            fail("请定义影响因素与响应指标")
        if any(not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip()
               for key in ("name", "symbol", "range", "calculation")) for item in definitions):
            fail("变量需要明确名称、符号、范围和计算口径")
    if not intake.get("planConfirmedAt"):
        fail("请确认完整方案后保存快照")
