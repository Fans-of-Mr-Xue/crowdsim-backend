"""Typed normalization and evidence addressing for the current getResult format."""

from __future__ import annotations

import hashlib
import json
import math
from itertools import product


class ContractError(ValueError):
    pass


def typed_key(value) -> str:
    """Keep bool/int/float distinct (Python True == 1 is not an input identity)."""
    if isinstance(value, dict):
        return json.dumps([[key, typed_key(value[key])] for key in sorted(value)], ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return json.dumps([typed_key(item) for item in value], ensure_ascii=False)
    return type(value).__name__ + ":" + json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def scalar_number(value):
    if isinstance(value, dict):
        value = value.get("value")
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ContractError("输入必须是成功的 getResult 返回对象")
    data = payload.get("result_query")
    if not isinstance(data, dict) or not data.get("experiment_run_id"):
        raise ContractError("缺少 result_query / experiment_run_id")
    selection = data.get("combination_selection") or {}
    order, levels = selection.get("factor_order"), selection.get("factor_levels")
    if not isinstance(order, list) or not order or len(set(order)) != len(order) or not isinstance(levels, dict):
        raise ContractError("缺少有效 factor_order / factor_levels")
    for factor in order:
        if not isinstance(levels.get(factor), list) or not levels[factor]:
            raise ContractError(f"因素 {factor} 没有水平")
        if len({typed_key(value) for value in levels[factor]}) != len(levels[factor]):
            raise ContractError(f"因素 {factor} 的水平重复")
    expected = [dict(zip(order, values)) for values in product(*(levels[key] for key in order))]
    groups = data.get("exp_results")
    if not isinstance(groups, list) or not groups:
        raise ContractError("exp_results 不能为空")
    expected_keys = {typed_key(item) for item in expected}
    seen, worlds = set(), []
    tick_count = 0
    for gi, group in enumerate(groups):
        plan = group.get("input") if isinstance(group, dict) else None
        if not isinstance(plan, dict) or set(plan) != set(order):
            raise ContractError(f"组合 {gi} 的 input 字段不匹配")
        key = typed_key(plan)
        if key not in expected_keys or key in seen:
            raise ContractError(f"组合 {gi} 重复或不在声明的全因子空间内")
        seen.add(key)
        trajectories = group.get("trajectories")
        if not isinstance(trajectories, list):
            raise ContractError(f"组合 {gi} 的 trajectories 必须是列表")
        for wi, trajectory in enumerate(trajectories):
            if not isinstance(trajectory, dict):
                raise ContractError("trajectory 必须是对象")
            ticks = trajectory.get("ticks")
            if not isinstance(ticks, list):
                raise ContractError("ticks 必须是列表")
            indexes = []
            for tick in ticks:
                idx = tick.get("tick_idx")
                if type(idx) is not int or idx < 0 or idx in indexes:
                    raise ContractError("tick_idx 必须是本世界内不重复的非负整数")
                indexes.append(idx)
                if not isinstance(tick.get("decision"), dict) or not isinstance(tick.get("world_state"), dict):
                    raise ContractError("tick 缺少 decision / world_state")
            if indexes != sorted(indexes):
                raise ContractError("ticks 必须按 tick_idx 升序排列")
            if trajectory.get("exp_plan") is not None and typed_key(trajectory["exp_plan"]) != key:
                raise ContractError("trajectory.exp_plan 与组合 input 不一致")
            error = trajectory.get("error") or group.get("error")
            eligible = bool(ticks) and not error and trajectory.get("finished") is True
            worlds.append({"world_ref": f"{data['experiment_run_id']}/g{gi:03d}/w{wi:02d}",
                           "group_index": gi, "world_index": wi, "input": plan,
                           "trajectory": trajectory, "eligible": eligible,
                           "exclusion_reason": str(error or ("轨迹为空或未完成" if not eligible else ""))})
            tick_count += len(ticks)
    plans = data.get("exp_plans")
    if not isinstance(plans, list) or {typed_key(item) for item in plans} != expected_keys or len(plans) != len(expected):
        raise ContractError("exp_plans 不是声明的完整全因子组合")
    return {"run_id": data["experiment_run_id"], "design_id": data.get("experiment_design_id"),
            "factor_order": order, "factor_levels": levels, "worlds": worlds,
            "groups": len(groups), "ticks": tick_count, "expected_groups": len(expected),
            "missing_groups": len(expected_keys - seen), "input_sha256": digest(payload)}


def read_field(obj: dict, field: str):
    value = obj
    remaining = field
    while remaining:
        if not isinstance(value, dict):
            raise ContractError(f"证据字段不存在：{field}")
        # World-state keys can themselves contain dots, e.g. 美国.military_power.
        if remaining in value:
            value = value[remaining]
            break
        part, separator, remaining = remaining.partition(".")
        if part not in value:
            raise ContractError(f"证据字段不存在：{field}")
        value = value[part]
        if not separator:
            break
    if (value is None or (isinstance(value, str) and not value.strip())
            or (isinstance(value, (list, dict)) and not value)):
        raise ContractError(f"证据字段为空：{field}")
    return value


def evidence_item(world: dict, tick_idx, field: str) -> dict:
    if tick_idx is None:
        if not field.startswith("rv_values."):
            raise ContractError("最终值证据只允许 rv_values 字段")
        source = world["trajectory"]
    else:
        if type(tick_idx) is not int:
            raise ContractError("证据 tick_idx 类型错误")
        source = next((tick for tick in world["trajectory"]["ticks"] if tick["tick_idx"] == tick_idx), None)
        if source is None:
            raise ContractError(f"证据回合不存在：{tick_idx}")
        allowed = {"scenario_content", "scenario_title", "decision.action", "decision.declaration", "decision.strategic_intent"}
        if field not in allowed and not field.startswith("world_state."):
            raise ContractError("证据字段不在允许范围内")
    value = read_field(source, field)
    eid = "ev-" + digest([world["world_ref"], tick_idx, field])[:20]
    return {"id": eid, "world_ref": world["world_ref"], "group_index": world["group_index"],
            "world_index": world["world_index"], "input": world["input"], "tick_idx": tick_idx,
            "field": field, "value": value,
            "text": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)}
