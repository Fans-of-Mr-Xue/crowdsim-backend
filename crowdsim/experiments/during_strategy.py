"""Class-2 fixed strategy proposals, immutable evidence and paired replay reports.

No model or simulator is called here. Generation uses the existing chat gateway;
only validated proposals can be confirmed through the dedicated replay endpoint.
"""
from copy import deepcopy
from collections import defaultdict
from statistics import mean

from crowdsim.domain.during_preset import preset_fingerprint, verify_bound_preset
from .contracts import ContractError, finite, validate_experiment_config

METHOD = "D2"
METRICS = {
    "firstCrowdingConfirmedAtSeconds": False,
    "crowdingDurationSeconds": True, "peakCrowdedCount": True,
    "peakHighRiskCount": True, "peakCoreDensity": True,
    "finalCompletionRate": False, "t95Seconds": True,
    "peakBlockedLowSpeedCount": True, "peakControlHeldCount": True,
}
ENTRY_LABELS = {"178411801#0": "西侧入口", "177931018#0": "南侧入口", "monument_m3": "西侧中部入口"}


def valid_row(row, scenario):
    d = row.get("during") or {}
    p = d.get("phenomenon") or {}
    return (row.get("status") == "completed" and row.get("formal", True)
            and d.get("run_complete") and d.get("population_valid")
            and d.get("preset_fingerprint") == scenario["presetFingerprint"]
            and p.get("coverage") == "complete" and type(p.get("occurred")) is bool)


def compact_row(row):
    return {k: deepcopy(row.get(k)) for k in (
        "runId", "seed", "crowdingOccurred", *METRICS)}


def control_constraints(preset):
    scope = preset["control_scope"]
    return {"actionType": "set_inflow_rate", "maxActions": 6, "timeStepSeconds": .5,
            "earliestStartSeconds": scope["earliest_apply_seconds"], "latestEndSeconds": scope["latest_end_seconds"],
            "entries": [{"id": entry, "label": ENTRY_LABELS.get(entry, entry)} for entry in scope["entry_ids"]],
            "rateRange": [0, 1], "methods": {"llm": True, "xw": False},
            "semantics": "rate 是相对入口标称吞吐的放行比例，不是停止人数的比例。同一入口时段不得重叠。到期自动释放；不改变总人数。"}


def validate_strategy(value, preset):
    if not isinstance(value, dict) or set(value) != {"name", "rationale", "expectedEffect", "limitations", "actions"}:
        raise ContractError("策略必须包含 name、rationale、expectedEffect、limitations、actions，且不能包含其他字段。")
    for key, maximum in (("name", 120), ("rationale", 2000), ("expectedEffect", 1500), ("limitations", 1500)):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > maximum:
            raise ContractError(f"策略 {key} 必须为非空文本，最多 {maximum} 字符。")
    constraints = control_constraints(preset)
    actions = value["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= constraints["maxActions"]:
        raise ContractError("策略必须包含 1～6 个限流时段。")
    entries = {entry["id"] for entry in constraints["entries"]}
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"entryId", "startSeconds", "endSeconds", "rate"}:
            raise ContractError("限流时段只能包含 entryId、startSeconds、endSeconds、rate。")
        if not isinstance(action["entryId"], str) or action["entryId"] not in entries:
            raise ContractError("策略包含当前场景不支持的入口。")
        start, end, rate = [finite(action[k]) for k in ("startSeconds", "endSeconds", "rate")]
        if (None in (start, end, rate) or not 0 <= rate <= 1
                or not constraints["earliestStartSeconds"] <= start < end <= constraints["latestEndSeconds"]
                or end - start < 1 or start * 2 != int(start * 2) or end * 2 != int(end * 2)):
            raise ContractError("限流比例须在 0～1，时间须为 0.5 秒的倍数、至少持续 1 秒，且在 1～800 秒内。")
    ordered = sorted(deepcopy(actions), key=lambda a: (a["startSeconds"], a["entryId"]))
    for i, left in enumerate(ordered):
        if any(left["entryId"] == right["entryId"] and right["startSeconds"] < left["endSeconds"] for right in ordered[i + 1:]):
            raise ContractError("同一入口的限流时段不能重叠。")
    return {**deepcopy(value), "actions": ordered}


def validate_generation(value):
    if not isinstance(value, dict) or set(value) != {"method", "model", "request", "rawResponse"} or value.get("method") != "llm":
        raise ContractError("本期仅支持大模型生成；所选策略生成方法尚未接入。")
    for key, limit in (("model", 200), ("request", 2000), ("rawResponse", 20000)):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > limit:
            raise ContractError(f"缺少有效的模型生成记录：{key}")
    return deepcopy(value)


class DuringStrategyService:
    def __init__(self, repository, reports):
        self.repository, self.reports = repository, reports

    def context(self, user, workspace, source_id):
        source = self.repository.get_experiment(user, workspace, source_id)
        if not source:
            raise LookupError("EXPERIMENT_NOT_FOUND")
        if source.get("methods") != ["C0"] or not source.get("scenario", {}).get("presetId") or source.get("mode") != "formal":
            raise ContractError("策略来源必须是已保存的正式第 1 类预设推演实验。")
        preset = verify_bound_preset(source["scenario"])
        runs = self.repository.list_runs(user, workspace, source_id)
        if (not runs or len(runs) != len(source["scenario"]["seedSet"])
                or any(r["status"] not in {"completed", "failed", "cancelled"} for r in runs)):
            raise ContractError("请等待来源推演实验的所有平行实验结束后再生成策略。")
        report = self.reports.generate(user, workspace, source_id)
        rows = sorted([r for r in report["runs"] if valid_row(r, source["scenario"])], key=lambda r: r["seed"])
        if not rows:
            raise ContractError("来源推演实验没有完整、有效的平行实验，无法生成或验证策略。")
        # Bounded descriptive timeline (at most 16 bins), never invented frames.
        bins = defaultdict(lambda: defaultdict(list))
        for row in rows:
            seen = set()
            for item in self.repository.list_records("observations", user, workspace, row["runId"], limit=100000):
                obs = item.get("payload", item)
                bucket = int(obs["simTimeSeconds"] // 120)
                if bucket in seen:
                    continue
                seen.add(bucket)
                d = obs.get("during") or {}
                s = d.get("sample") or {}
                for key, val in {"observedAtSeconds": obs["simTimeSeconds"], "coreCount": s.get("core_count"),
                                 "crowdedCount": (s["crowded_count"] + s["high_risk_count"]) if finite(s.get("crowded_count")) is not None and finite(s.get("high_risk_count")) is not None else None,
                                 "completionRate": d.get("completion_rate")}.items():
                    if finite(val) is not None:
                        bins[bucket][key].append(val)
        context = {"schemaVersion": 1, "sourceExperimentId": source_id, "researchQuestion": source.get("researchQuestion", source["name"]),
                   "presetId": source["scenario"]["presetId"], "presetFingerprint": source["scenario"]["presetFingerprint"],
                   "population": source["scenario"]["population"], "durationSeconds": source["scenario"]["durationSeconds"],
                   "phenomenonDefinition": preset["phenomenon"], "constraints": control_constraints(preset),
                   "runs": [compact_row(r) for r in rows],
                   "excludedRuns": [{"runId": r["runId"], "seed": r["seed"], "status": r["status"]} for r in report["runs"] if r not in rows],
                   "timeline": [{"bucketStartSeconds": b * 120, **{k: mean(v) for k, v in values.items()}} for b, values in sorted(bins.items())],
                   "limitations": "仅使用有效平行实验，同种子从头验证；这些种子也用于生成策略，比较仅作描述性验证，不证明泛化改善。行程完成不代表到达安全区。"}
        return {**context, "sourceFingerprint": preset_fingerprint(context)}

    def preview(self, user, workspace, source_id, payload):
        context = self.context(user, workspace, source_id)
        if payload.get("sourceFingerprint") != context["sourceFingerprint"]:
            raise ContractError("来源证据已改变，请重新生成策略。")
        source = self.repository.get_experiment(user, workspace, source_id)
        strategy = validate_strategy(payload.get("strategy"), source["scenario"]["duringPreset"])
        generation = validate_generation(payload.get("generation"))
        candidate = {"sourceExperimentId": source_id, "sourceFingerprint": context["sourceFingerprint"],
                     "strategy": strategy, "generation": generation}
        return {**candidate, "fingerprint": preset_fingerprint(candidate), "sourceContext": context}

    def create_replay(self, user, workspace, source_id, payload):
        candidate = self.preview(user, workspace, source_id, payload)
        if payload.get("confirmedFingerprint") != candidate["fingerprint"]:
            raise ContractError("请先确认当前策略；策略或来源发生变化后必须重新确认。")
        if not payload.get("clientRequestId"):
            raise ContractError("策略验证必须提供幂等提交标识。")
        source = self.repository.get_experiment(user, workspace, source_id)
        context = self.context(user, workspace, source_id)
        scenario = deepcopy(source["scenario"])
        scenario["seedSet"] = [r["seed"] for r in context["runs"]]
        # A source may require more seeds than remain valid. Record the excluded
        # seeds explicitly and do not borrow seeds from another experiment.
        scenario.pop("statisticalValidation", None)
        config = validate_experiment_config({
            "name": candidate["strategy"]["name"], "clientRequestId": payload["clientRequestId"],
            "researchQuestion": source.get("researchQuestion", source["name"]),
            "methods": [METHOD], "mode": "formal", "scenario": scenario,
            "observationIntervalSeconds": source["observationIntervalSeconds"],
            "evaluationWindowSeconds": source["evaluationWindowSeconds"],
            "duringReplay": {**candidate, "sourceContext": context, "sourceClarificationSnapshot": source.get("clarificationSnapshot")},
            "controllerConfig": {METHOD: {"strategy": candidate["strategy"], "strategyFingerprint": candidate["fingerprint"]}},
        }, allow_during_replay=True)
        return self.repository.create_experiment(user, workspace, config)


def validate_replay_binding(experiment):
    """Reject tampered stored replays before queueing any simulation work."""
    replay = experiment.get("duringReplay") or {}
    scenario = experiment["scenario"]
    strategy = validate_strategy(replay.get("strategy"), scenario["duringPreset"])
    generation = validate_generation(replay.get("generation"))
    context = replay.get("sourceContext") or {}
    source_payload = {k: v for k, v in context.items() if k != "sourceFingerprint"}
    candidate = {"sourceExperimentId": replay.get("sourceExperimentId"), "sourceFingerprint": replay.get("sourceFingerprint"),
                 "strategy": strategy, "generation": generation}
    if (experiment.get("methods") != [METHOD] or experiment.get("mode") != "formal"
            or preset_fingerprint(candidate) != replay.get("fingerprint")
            or preset_fingerprint(source_payload) != replay.get("sourceFingerprint")
            or context.get("sourceExperimentId") != replay.get("sourceExperimentId")
            or context.get("presetFingerprint") != scenario["presetFingerprint"]
            or scenario["seedSet"] != [r["seed"] for r in context.get("runs", [])]
            or experiment.get("controllerConfig") != {METHOD: {"strategy": strategy, "strategyFingerprint": replay["fingerprint"]}}):
        raise ContractError("策略重跑配置与已确认来源不一致。")


def replay_comparison(experiment, rows, repository, user, workspace):
    replay = experiment["duringReplay"]
    baseline = {r["seed"]: r for r in replay["sourceContext"]["runs"]}
    pairs, excluded = [], []
    expected = len(replay["strategy"]["actions"])
    for row in rows:
        decisions = repository.list_records("decisions", user, workspace, row["runId"], limit=100000)
        acks = repository.list_records("acks", user, workspace, row["runId"], limit=100000)
        ids = [a["actionId"] for d in decisions for a in d["payload"].get("actions", [])]
        applied = {a["payload"].get("actionId") for a in acks if a["payload"].get("status") == "applied"}
        complete = len(ids) == expected and len(set(ids)) == expected and set(ids) <= applied
        row["strategyExecutionComplete"] = complete
        row["strategyActions"] = []
        for record in decisions:
            decision = record["payload"]
            schedule = decision.get("algorithmTrace", {}).get("schedule", [])
            for index, action in enumerate(decision.get("actions", [])):
                acknowledgements = [a["payload"] for a in acks if a["payload"].get("actionId") == action["actionId"]]
                final_ack = next((a for a in reversed(acknowledgements) if a.get("status") != "queued"), {})
                planned = schedule[index] if index < len(schedule) else {}
                row["strategyActions"].append({"actionId": action["actionId"], "entryId": action.get("target", {}).get("entryId"),
                    "plannedStartSeconds": planned.get("startSeconds"), "plannedEndSeconds": planned.get("endSeconds"),
                    "dispatchedAtSeconds": decision.get("validFrom"), "appliedAtSeconds": final_ack.get("appliedAt"),
                    "status": final_ack.get("status", "pending"), "error": final_ack.get("error")})
        if not valid_row(row, experiment["scenario"]) or not complete or row["seed"] not in baseline:
            excluded.append({"runId": row["runId"], "seed": row["seed"], "status": row["status"],
                             "reason": "观测未完整有效" if not valid_row(row, experiment["scenario"]) else "策略动作未全部获得执行回执"})
            continue
        before, after = baseline[row["seed"]], compact_row(row)
        pairs.append({"seed": row["seed"], "baseline": before, "candidate": after,
                      "differences": {k: after[k] - before[k] if finite(after[k]) is not None and finite(before[k]) is not None else None for k in METRICS}})
    metrics = []
    for key, lower in METRICS.items():
        values = [p["differences"][key] for p in pairs if p["differences"][key] is not None]
        metrics.append({"metric": key, "pairCount": len(values), "meanDifference": mean(values) if values else None,
                        "lowerIsBetter": lower, "direction": "candidate_minus_baseline"})
    return {"sourceExperimentId": replay["sourceExperimentId"], "strategyFingerprint": replay["fingerprint"],
            "plannedPairCount": len(baseline), "validPairCount": len(pairs), "pairs": pairs,
            "occurrence": {side: {"count": sum(p[side]["crowdingOccurred"] is True for p in pairs),
                                  "frequency": sum(p[side]["crowdingOccurred"] is True for p in pairs) / len(pairs) if pairs else None}
                           for side in ("baseline", "candidate")},
            "excludedRuns": excluded, "metrics": metrics, "descriptiveOnly": True,
            "limitations": "差值=策略实验−基线实验，仅使用完整观测且全部动作执行成功的同种子配对。95% 未达到时不计算时间差；需同时看聚集、行程完成和外围受阻，不能仅凭核心人数下降宣布改善。生成与验证共用种子，不作统计推断。"}
