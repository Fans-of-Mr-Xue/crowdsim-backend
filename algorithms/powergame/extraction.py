"""Nonempty structured-output guard, with no implicit LLM-to-rules fallback."""

from __future__ import annotations

import json
import re
import time

from .catalog import NODE_BY_ID, NODES, TEXT_RULES, TIER_ORDER
from .contract import ContractError, digest, evidence_item


class ExtractionError(ValueError):
    def __init__(self, message, attempts=None):
        super().__init__(message)
        self.attempts = attempts or []


def decode_object(raw):
    if isinstance(raw, str):
        text = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
            text = re.sub(r"\s*```$", "", text)
        try:
            raw = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, TypeError) as exc:
            raise ExtractionError("输出不是有效 JSON 对象") from exc
    if not isinstance(raw, dict) or not raw:
        raise ExtractionError("JSON 对象不能为空")
    return raw


def validate_extraction(raw, world: dict, nodes=None) -> list[dict]:
    raw = decode_object(raw)
    node_by_id = {node["id"]: node for node in nodes} if nodes is not None else NODE_BY_ID
    if raw.get("world_ref") != world["world_ref"]:
        raise ExtractionError("world_ref 必须与当前世界完全一致")
    relations = raw.get("relations")
    if not isinstance(relations, list) or not 1 <= len(relations) <= 32:
        raise ExtractionError("relations 必须包含 1 到 32 条有证据的候选关系")
    merged = {}
    for relation in relations:
        if not isinstance(relation, dict):
            raise ExtractionError("关系必须是对象")
        source, target, direction = relation.get("source"), relation.get("target"), relation.get("relation")
        if not isinstance(source, str) or not isinstance(target, str) or source not in node_by_id or target not in node_by_id:
            raise ExtractionError("关系节点不在固定词表内")
        if TIER_ORDER[node_by_id[source]["tier"]] >= TIER_ORDER[node_by_id[target]["tier"]]:
            raise ExtractionError("候选关系必须沿实验因素→行动→中介→结果方向，禁止自环或逆向")
        if direction not in ("promotes", "inhibits"):
            raise ExtractionError("relation 必须是 promotes 或 inhibits")
        explanation = relation.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 1000:
            raise ExtractionError("explanation 必须是非空短文本")
        refs = relation.get("evidence")
        if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
            raise ExtractionError("每条关系必须有 1 到 8 条真实证据引用")
        resolved = []
        for ref in refs:
            if (not isinstance(ref, dict) or not isinstance(ref.get("field"), str)
                    or (type(ref.get("tick_idx")) is not int
                        and not (ref.get("tick_idx") is None and ref["field"].startswith("rv_values.")))):
                raise ExtractionError("证据必须包含整数 tick_idx 和字符串 field")
            try:
                resolved.append(evidence_item(world, ref["tick_idx"], ref["field"]))
            except ContractError as exc:
                raise ExtractionError(str(exc)) from exc
        key = (source, target, direction)
        if key not in merged:
            merged[key] = {"source": source, "target": target, "relation": direction,
                           "explanation": explanation.strip(), "evidence_items": {}}
        for item in resolved:
            merged[key]["evidence_items"][item["id"]] = item
    return [{**value, "evidence_items": list(value["evidence_items"].values())} for value in merged.values()]


def build_prompt(world: dict, repair_errors: str = "", *, nodes=None, synthetic=True) -> str:
    ticks = [{"tick_idx": tick["tick_idx"], "camp": tick["camp"],
              "decision": {key: str(tick["decision"].get(key, ""))[:1200] for key in ("action", "target", "declaration", "strategic_intent")},
              "scenario_content": tick.get("scenario_content", "")[:2500], "world_state": tick["world_state"]}
             for tick in world["trajectory"]["ticks"]]
    shape = {"world_ref": world["world_ref"], "relations": [{"source": "节点id", "target": "节点id",
             "relation": "promotes 或 inhibits", "explanation": "轨迹支持的非空解释",
             "evidence": [{"tick_idx": 0, "field": "scenario_content"}]}]}
    return (
        "你是轨迹关系标注器。输入是" + ("模拟示例" if synthetic else "真实保存的干预仿真实验") + "，结论限于仿真。只输出一个非空JSON对象，不输出思考或Markdown。\n"
        "从轨迹提取1到32条有原文依据的候选传导关系；不是因果验证，不要生成效应、概率或置信度。\n"
        "只用词表中的id，关系方向必须从较左层到较右层：context→treatment→mediator→outcome，可以跳层。\n"
        "每条必须有非空explanation和至少一项evidence；tick_idx必须实际存在；field用scenario_content、decision.action或world_state.真实键。\n"
        "同一世界同一关系合并引用。不要用未采样备用事件或不存在的回合充当证据。\n"
        "最终响应值可以引用 {\"tick_idx\":null,\"field\":\"rv_values.原始指标名\"}；指标名与实际键完全一致。\n"
        + "输出结构：" + json.dumps(shape, ensure_ascii=False) + "\n"
        + "本次实验词表：" + json.dumps(nodes if nodes is not None else NODES, ensure_ascii=False) + "\n"
        + "当前世界：" + world["world_ref"] + "\n"
        + "实验因素：" + json.dumps(world["input"], ensure_ascii=False) + "\n"
        + "逐回合数据：" + json.dumps(ticks, ensure_ascii=False) + "\n"
        + "最终响应变量：" + json.dumps(world["trajectory"].get("rv_values", {}), ensure_ascii=False) + "\n"
        + ("上次输出未通过校验，请修复这些错误：" + repair_errors if repair_errors else "")
    )


class RulesExtractor:
    mode = "rules"

    def extract(self, world: dict) -> dict:
        relations = []
        for phrase, source, target, direction in TEXT_RULES:
            refs = [{"tick_idx": tick["tick_idx"], "field": "scenario_content"}
                    for tick in world["trajectory"]["ticks"] if phrase in tick.get("scenario_content", "")]
            if refs:
                relations.append({"source": source, "target": target, "relation": direction,
                                  "explanation": f"规则在已采样回合文本中匹配到：{phrase}。", "evidence": refs[:8]})
        raw = {"world_ref": world["world_ref"], "relations": relations}
        return {"relations": validate_extraction(raw, world), "attempts": [], "method": "explicit_text_rules"}


class GuardedLLMExtractor:
    mode = "llm"

    def __init__(self, caller, *, max_attempts=3, world_budget_seconds=90.0, nodes=None, synthetic=True):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5 or world_budget_seconds <= 0:
            raise ValueError("无效重试或预算参数")
        self.caller = caller
        self.max_attempts = max_attempts
        self.world_budget_seconds = world_budget_seconds
        self.nodes = nodes
        self.synthetic = synthetic

    def extract(self, world: dict) -> dict:
        started = time.monotonic()
        errors, attempts = "", []
        for number in range(1, self.max_attempts + 1):
            remaining = self.world_budget_seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            raw = None
            try:
                raw = self.caller(build_prompt(world, errors, nodes=self.nodes, synthetic=self.synthetic), timeout=remaining)
                if time.monotonic() - started > self.world_budget_seconds:
                    raise ExtractionError("本世界分析超过总时长预算")
                relations = validate_extraction(raw, world, self.nodes)
                attempts.append({"attempt": number, "status": "accepted", "response_chars": len(str(raw)),
                                 "response_sha256": digest(raw), "relations": len(relations)})
                return {"relations": relations, "attempts": attempts, "method": "llm_validated_json"}
            except ExtractionError as exc:
                errors = str(exc)
            except Exception as exc:
                # Do not persist exception URLs, credentials or headers in artifacts.
                errors = f"模型请求失败（{type(exc).__name__}）"
            attempts.append({"attempt": number, "status": "rejected", "error": errors,
                             "response_chars": len(str(raw)) if raw is not None else 0})
        raise ExtractionError(errors or "本世界分析预算耗尽", attempts)
