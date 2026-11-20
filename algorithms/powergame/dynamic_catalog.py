"""Build the real-run vocabulary from saved fields and trace-backed action labels."""
from __future__ import annotations

import json
import math
import time

from .contract import digest, evidence_item, scalar_number, typed_key
from .extraction import ExtractionError, decode_object


def unwrap_value(value):
    if isinstance(value, dict):
        for key in ("value", "score", "label", "result", "outcome"):
            if key in value:
                return value[key]
        return None
    return value


def metric_value(raw, meta):
    value = unwrap_value(raw)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if meta.get("kind", "numeric") == "category":
        if meta.get("known_categories") is not None and typed_key(value) not in meta["known_categories"]:
            return None
        return 100.0 if typed_key(value) == typed_key(meta["category"]) else 0.0
    return scalar_number(value)


def node_id(kind, key):
    return kind + "-" + digest(key)[:14]


def discover_actions(data, caller, *, budget=90.0):
    worlds = {world["world_ref"]: world for world in data["worlds"] if world["eligible"]}
    observations = [{"world_ref": world["world_ref"], "tick_idx": tick["tick_idx"],
                     "action": str(tick["decision"].get("action", ""))[:500]}
                    for world in worlds.values() for tick in world["trajectory"]["ticks"]
                    if str(tick["decision"].get("action", "")).strip()]
    if not observations:
        raise ExtractionError("轨迹中没有可分析的行动")
    if len(observations) > 64:
        observations = [observations[round(index * (len(observations) - 1) / 63)] for index in range(64)]
    allowed = {(row["world_ref"], row["tick_idx"]) for row in observations}
    prompt = (
        '为这次干预仿真实验归并1到8类实际出现的行动。仅输出非空JSON对象，格式为'
        '{"actions":[{"name":"简短行动类别","explanation":"类别含义",'
        '"evidence":[{"world_ref":"原始世界编号","tick_idx":0}]}]}。'
        '每类至少一个真实引用，仅可用下面提供的编号；不要复制某个预设场景的节点，不要生成概率。\n'
        + json.dumps(observations, ensure_ascii=False)
    )
    started, errors, attempts = time.monotonic(), "", []
    for number in range(1, 4):
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            break
        raw = None
        try:
            raw = caller(prompt + ("\n修复错误：" + errors if errors else ""), timeout=remaining)
            if time.monotonic() - started > budget:
                raise ExtractionError("行动词表生成超过预算")
            parsed = decode_object(raw)
            actions = parsed.get("actions")
            if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
                raise ExtractionError("actions 必须有1到8个非空类别")
            nodes, seen = [], set()
            for action in actions:
                if not isinstance(action, dict):
                    raise ExtractionError("行动类别必须是对象")
                name, explanation, refs = action.get("name"), action.get("explanation"), action.get("evidence")
                if not isinstance(name, str) or not name.strip() or len(name) > 40 or name.strip() in seen:
                    raise ExtractionError("行动类别名必须简短、非空且不重复")
                if not isinstance(explanation, str) or not explanation.strip() or not isinstance(refs, list) or not refs:
                    raise ExtractionError("每类必须有非空解释和证据")
                evidence = []
                for ref in refs[:8]:
                    if (not isinstance(ref, dict) or type(ref.get("tick_idx")) is not int
                            or (ref.get("world_ref"), ref["tick_idx"]) not in allowed):
                        raise ExtractionError("行动类别引用了未提供的世界或回合")
                    evidence.append(evidence_item(worlds[ref["world_ref"]], ref["tick_idx"], "decision.action"))
                seen.add(name.strip())
                nodes.append({"id": node_id("action", name.strip()), "name": name.strip(), "tier": "treatment",
                              "description": explanation.strip()[:300], "catalog_evidence": evidence})
            attempts.append({"attempt": number, "status": "accepted", "response_chars": len(str(raw)), "actions": len(nodes)})
            return nodes, attempts
        except Exception as exc:
            errors = str(exc) if isinstance(exc, ExtractionError) else f"模型请求失败（{type(exc).__name__}）"
            attempts.append({"attempt": number, "status": "rejected", "error": errors, "response_chars": len(str(raw)) if raw is not None else 0})
    raise ExtractionError(errors or "行动词表生成失败", attempts)


def build_catalog(data, design, action_nodes):
    factors, outcomes, nodes = {}, {}, []
    for key in data["factor_order"]:
        nid = node_id("factor", key)
        factors[key] = {"label": key, "levels": data["factor_levels"][key], "node_id": nid}
        nodes.append({"id": nid, "name": key, "tier": "context", "source_key": key})
    nodes.extend(action_nodes)
    eligible = [world for world in data["worlds"] if world["eligible"]]
    observed = dict.fromkeys(key for world in eligible for tick in world["trajectory"]["ticks"]
                             for key, value in tick["world_state"].items() if type(value) in (str, int, float, bool))
    state_meta = design.get("world_state_meta") or {}
    state_keys = list(dict.fromkeys([key for key in state_meta if key in observed] + list(observed)))
    for key in state_keys[:16]:
        meta = state_meta.get(key) or {}
        if not meta and "." in key:
            meta = state_meta.get(key.split(".", 1)[1]) or {}
        label = meta.get("label") or key
        nodes.append({"id": node_id("state", key), "name": label, "tier": "mediator", "source_key": key,
                      "description": str(meta.get("explain") or "")[:240]})
    rv_keys = dict.fromkeys(key for world in eligible for key in world["trajectory"].get("rv_values", {}))
    for key in rv_keys:
        values = [unwrap_value(world["trajectory"].get("rv_values", {}).get(key)) for world in eligible]
        values = [value for value in values if value is not None and value != ""]
        if not values:
            continue
        binding = (design.get("rv_binding") or {}).get(key) or {}
        numeric = all(type(value) in (int, float) and math.isfinite(value) for value in values)
        if numeric:
            unit = binding.get("unit") or ("分" if str(binding.get("compute_type", "")).startswith("llm_score") else "数值单位")
            definitions = [{"label": key, "kind": "numeric", "unit": unit}]
        else:
            categories = list({typed_key(value): value for value in values if type(value) in (str, bool)}.values())
            definitions = [{"label": f"{key}：{value}", "kind": "category", "category": value, "unit": "百分点",
                            "known_categories": [typed_key(item) for item in categories]} for value in categories[:8]]
        for definition in definitions:
            nid = node_id("outcome", [key, definition.get("category")])
            outcomes[nid] = {**definition, "node_id": nid, "source_key": key}
            nodes.append({"id": nid, "name": definition["label"], "tier": "outcome", "source_key": key,
                          "metric": {**definition, "source_key": key}})
    if not outcomes:
        raise ExtractionError("本次轨迹缺少可读取的最终响应变量")
    return {"factors": factors, "outcomes": outcomes, "nodes": nodes,
            "omitted_state_keys": state_keys[16:], "source": "saved_run_and_start_design_snapshot"}
