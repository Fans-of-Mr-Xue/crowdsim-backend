"""按轨迹前缀估计结局概率，再对固定的完整序列做关键节点解读。"""

from __future__ import annotations

import json
import math
from typing import Callable


PROMPT = """你正在评估一个危机模拟世界在当前回合结束后的未来走向。
只使用下面提供的起始事件、固定条件和截至当前回合的记录，不得补入已知历史结局。
问题是：假设未来最终进入某个明确结局，哪一种结局会首先出现？
这不是预测剩余 tick 内能否结束；不输出悬而未决、未达终局或阶段性优势。
根据各类别定义输出完整概率分布，各概率在 0 到 1 之间，合计为 1。
概率可以随局势上升或下降，不要为了让曲线好看而单调变化，也不要自动把最后一轮设为 100%。

明确结局类别：
{categories}

截至当前回合的信息：
{evidence}

只返回一个 JSON 对象：
{{"probabilities": {{"类别 key": 概率}}, "reason": "结合本轮信息，用一句中文解释判断"}}
"""


KEY_POINTS_PROMPT = """你正在事后解读一个危机模拟世界的完整结局概率曲线。
概率已经确定，不得重新估计、修改概率，也不得补入已知历史结局或未记录的事件。
综合所有结局曲线，选择至多 3 个最有意义的关键回合；没有明显变化时返回空列表，不要凑数。
节点类型：surge（明显上升）、drop（明显下降）、trend_reversal（趋势反转）、lead_change（最可能结局发生交换）。
趋势反转应定位到开始转向的回合；不要把普通最高点、最低点自动当成关键节点。
同一回合只选一次，outcome_keys 填入与这次变化有关的结局，可包含多个，不能假定只有两种结局。
只选择本回合与紧邻的上一回合都有有效概率的节点；首回合不能作为变化节点。

每个节点用中文写清楚：具体行动或事件 → 如何改变局势或各方选择 → 为什么模型的结局判断随之变化。
解释的是模型估计变化的可能原因，不是已经验证的因果关系。缺少事件证据时如实说明，不能编造理由。
可以用后续走势判断转折是否持续，但解释本回合变化所引用的事件必须发生在本回合或更早；
evidence_tick_indices 只填写这些依据所在的真实 tick_idx，不得引用后续回合作为此前变化的原因。
所有 tick_idx 都使用输入中的原始编号，不要自行加一。按回合先后输出。
curve_summary 用一两句话概括整体走势；没有明显关键节点时也说明整体情况。

明确结局类别：
{categories}

完整轨迹与各回合原始预测（probabilities 为 null 表示该回合估计缺失，不得补造）：
{evidence}

只返回一个 JSON 对象：
{{"curve_summary": "整体走势概括", "key_points": [
  {{"tick_idx": 真实回合编号, "type": "trend_reversal", "title": "简短中文标题",
    "outcome_keys": ["类别 key"], "reason": "具体事件及其影响解释",
    "evidence_tick_indices": [依据回合编号]}}
]}}
"""


def _read_distribution(response: dict, keys: list[str]) -> tuple[dict, str]:
    probabilities = response["probabilities"]
    if set(probabilities) != set(keys):
        raise ValueError("预测类别与本场景配置不一致")
    values = [probabilities[key] for key in keys]
    if any(type(value) not in (int, float) or not math.isfinite(value)
           or not 0 <= value <= 1 for value in values):
        raise ValueError("结局概率必须为 0 到 1 的有限数值")
    total = sum(values)
    if abs(total - 1) > 0.02:
        raise ValueError("结局概率合计必须为 1")
    reason = response["reason"].strip()
    if not reason:
        raise ValueError("缺少本回合的预测理由")
    # 只消除模型小数舍入误差，不改变有效概率分布的相对关系。
    return {key: probabilities[key] / total for key in keys}, reason


def estimate_world_outcome_forecasts(
    groups: list[dict], categories: list[dict], start_event: str,
    llm_factory: Callable,
) -> dict:
    """每个有效世界、每个回合各调用一次模型，结果随观察分析统一保存。"""
    result = {"categories": categories, "worlds": {}}
    keys = [category["key"] for category in categories]
    categories_text = json.dumps(categories, ensure_ascii=False)
    for group in groups:
        for trajectory in group.get("trajectories", []):
            if trajectory.get("error") or not trajectory.get("ticks"):
                continue
            snapshots = {item["tick_idx"]: item["rv_values"]
                         for item in trajectory.get("rv_snapshots", [])}
            rounds = []
            points = []
            for tick in trajectory["ticks"]:
                tick_idx = tick["tick_idx"]
                rounds.append({
                    "tick_idx": tick_idx,
                    **{key: tick[key] for key in (
                        "camp", "decision", "scenario_title", "scenario_content", "world_state",
                    ) if key in tick},
                    "rv_values": snapshots.get(tick_idx, {}),
                })
                evidence = {
                    "start_event": trajectory.get("start_event") or start_event,
                    "input_plan": group.get("input_plan", {}),
                    "rounds": rounds,
                }
                prompt = PROMPT.format(
                    categories=categories_text,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                )
                point = {"tick_idx": tick_idx}
                try:
                    # 独立客户端避免上个回合的对话历史混入当前估计。
                    response = llm_factory().chat(prompt, parse_json=True)
                    probabilities, reason = _read_distribution(response, keys)
                    point.update(probabilities=probabilities, reason=reason)
                except Exception as exc:
                    point.update(probabilities=None, reason="", error=str(exc))
                points.append(point)
            result["worlds"][trajectory["world_id"]] = {"points": points}
    return result


def analyse_forecast_key_points(
    forecasts: dict, groups: list[dict], start_event: str, llm_factory: Callable,
) -> None:
    """所有概率生成后，每个世界额外调用一次模型；只追加解读，不修改 points。"""
    categories = forecasts["categories"]
    category_keys = {item["key"] for item in categories}
    for group in groups:
        for trajectory in group.get("trajectories", []):
            world = forecasts["worlds"].get(trajectory["world_id"])
            if world is None:
                continue
            world.update(key_points=[], curve_summary="")
            points = world["points"]
            if sum(point["probabilities"] is not None for point in points) < 2:
                world["curve_summary"] = "有效概率点不足，暂无法识别关键节点。"
                continue
            point_by_tick = {point["tick_idx"]: point for point in points}
            snapshots = {item["tick_idx"]: item["rv_values"]
                         for item in trajectory.get("rv_snapshots", [])}
            rounds = [{
                "tick_idx": tick["tick_idx"],
                **{key: tick[key] for key in (
                    "camp", "decision", "scenario_title", "scenario_content", "world_state",
                ) if key in tick},
                "rv_values": snapshots.get(tick["tick_idx"], {}),
                "forecast": {key: point_by_tick[tick["tick_idx"]][key]
                             for key in ("probabilities", "reason")},
            } for tick in trajectory["ticks"]]
            evidence = {
                "start_event": trajectory.get("start_event") or start_event,
                "input_plan": group.get("input_plan", {}), "rounds": rounds,
            }
            try:
                response = llm_factory().chat(KEY_POINTS_PROMPT.format(
                    categories=json.dumps(categories, ensure_ascii=False),
                    evidence=json.dumps(evidence, ensure_ascii=False),
                ), parse_json=True)
                summary = response["curve_summary"].strip()
                selections = response["key_points"]
                if not summary or not isinstance(selections, list) or len(selections) > 3:
                    raise ValueError("关键节点解读需包含走势概括及至多三个节点")
                positions = {point["tick_idx"]: index for index, point in enumerate(points)}
                key_points = []
                selected_ticks = set()
                for selection in selections:
                    tick_idx = selection["tick_idx"]
                    index = positions[tick_idx]
                    if index == 0 or tick_idx in selected_ticks:
                        raise ValueError("关键节点必须是非首回合且不能重复")
                    previous, current = points[index - 1], points[index]
                    if previous["probabilities"] is None or current["probabilities"] is None:
                        raise ValueError("关键节点及其上一回合必须有有效概率")
                    kind = selection["type"]
                    keys = selection["outcome_keys"]
                    title, reason = selection["title"].strip(), selection["reason"].strip()
                    refs = selection["evidence_tick_indices"]
                    if kind not in {"surge", "drop", "trend_reversal", "lead_change"}:
                        raise ValueError("关键节点类型无效")
                    if not keys or len(set(keys)) != len(keys) or not set(keys) <= category_keys:
                        raise ValueError("关键节点引用了无效或重复的结局类别")
                    if not title or not reason or not refs or any(
                        ref not in positions or positions[ref] > index for ref in refs
                    ):
                        raise ValueError("关键节点需要标题、理由以及本回合或更早的真实依据")
                    key_points.append({
                        "tick_idx": tick_idx, "type": kind, "title": title, "reason": reason,
                        "evidence_tick_indices": sorted(set(refs), key=positions.get),
                        "changes": [{
                            "outcome_key": key, "previous_tick_idx": previous["tick_idx"],
                            "from_probability": previous["probabilities"][key],
                            "to_probability": current["probabilities"][key],
                            "delta": current["probabilities"][key] - previous["probabilities"][key],
                        } for key in keys],
                    })
                    selected_ticks.add(tick_idx)
                world.update(curve_summary=summary, key_points=sorted(
                    key_points, key=lambda point: positions[point["tick_idx"]]))
            except Exception as exc:
                # 解读失败仍保留原始概率曲线，不重试或编造节点。
                world["key_points_error"] = str(exc)
