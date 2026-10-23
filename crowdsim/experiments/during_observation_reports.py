"""Persist explicit class-one interpretations alongside their measured evidence.

No model or simulator is called here. A prepared source prevents a delayed model
reply from replacing a newer interpretation or surviving a destructive rerun.
"""
from copy import deepcopy
import re
import uuid

from .contracts import ContractError, public_time
from .during_observation_analysis import analysis_skill, build_observation_analysis, REPORT_SECTIONS


def _experiment(repository, user_id, workspace_id, experiment_id):
    experiment = repository.get_experiment(user_id, workspace_id, experiment_id)
    if not experiment:
        raise LookupError("EXPERIMENT_NOT_FOUND")
    binding = experiment.get("duringObservation") or {}
    if not binding or workspace_id != f"during-{binding.get('conversationId')}":
        raise ContractError("实验总览报告仅用于当前事中第一类实验。")
    return experiment


def _fields(experiment, kind):
    if kind not in ("interpretation", "conclusion"):
        raise ContractError("不支持的实验报告产物。")
    if kind == "conclusion":
        requirement = experiment["duringObservation"].get("requirement") or {}
        if "concise-simulation-summary" not in requirement.get("observation", {}).get("metric_ids", []):
            raise ContractError("本组确认需求未选择“一句话推演结论”。")
    return kind, f"{kind}Source"


def _validate_conclusion(text):
    endings = list(re.finditer(r"[。！？!?]|\.(?![0-9])", text))
    if (len(text) > 200 or "\n" in text or "\r" in text or len(endings) > 1
            or (endings and text[endings[0].end():].strip())):
        raise ContractError("一句话推演结论须为单句、单行且不超过 200 字，请重新生成。")


def _format_analysis_text(text, kind):
    text = text.strip()
    if kind == "conclusion":
        return re.sub(r"^(?:问题回答|一句话推演结论|结论)[：:]\s*", "", text)
    # Formatting the text itself leaves the existing pre-wrap UI untouched.
    labels = "|".join(re.escape(label) for label in REPORT_SECTIONS)
    return re.sub(r"\s*(" + labels + r")", lambda m: "\n\n" + m.group(1), text).strip()


def interpretation(repository, user_id, workspace_id, experiment_id, *, kind="interpretation"):
    with repository._lock:
        experiment = _experiment(repository, user_id, workspace_id, experiment_id)
        result_key, _ = _fields(experiment, kind)
        row = repository.get_report(user_id, workspace_id, experiment_id) or {}
        saved = row.get(result_key)
        return saved if saved and saved["executionVersion"] == experiment.get("executionVersion", 0) else None


def _store(repository, query, values):
    # Preserve the disk authority if persistence fails; a retry must flush again.
    document = next(row for row in repository._memory["reports"] if all(row.get(key) == value for key, value in query.items()))
    previous = deepcopy(document)
    try:
        repository._update("reports", query, values)
    except Exception:
        document.clear()
        document.update(previous)
        raise


def write_interpretation(repository, reports, user_id, workspace_id, experiment_id, payload):
    with repository._lock:
        experiment = _experiment(repository, user_id, workspace_id, experiment_id)
        kind = payload.get("kind", "interpretation")
        result_key, source_key = _fields(experiment, kind)
        version = experiment.get("executionVersion", 0)
        if type(payload.get("executionVersion")) is not int or payload["executionVersion"] != version:
            raise ContractError("实验已重新运行，请刷新后重新生成报告。")
        query = {"user_id": str(user_id), "workspace_id": str(workspace_id), "experiment_id": str(experiment_id)}
        if payload.get("action") == "prepare":
            report = reports.generate(user_id, workspace_id, experiment_id)
            if not report["validRunCount"]:
                raise ContractError("暂无有效完成轮次，暂不能生成大模型解读。")
            facts = {k: deepcopy(report[k]) for k in ("experimentId", "reportVersion", "binding", "expectedRunCount", "validRunCount", "aggregate", "limitations")}
            facts["runs"] = [{k: deepcopy(run.get(k)) for k in
                              ("runId", "seed", "status", "valid", "excludedReason", "metrics", "personCount")} for run in report["runs"]]
            requirement = experiment["duringObservation"]["requirement"]
            facts["researchQuestion"] = requirement.get("project", {}).get("description", "")
            facts["selectedMetricIds"] = deepcopy(requirement["observation"]["metric_ids"])
            facts["observationEvidenceVersion"] = report["observationEvidenceVersion"]
            analysis = build_observation_analysis(repository, user_id, workspace_id, experiment, report)
            source = {"sourceId": uuid.uuid4().hex, "kind": kind, "executionVersion": version,
                      "reportVersion": report["reportVersion"], "facts": facts,
                      "analysis": analysis, "skill": analysis_skill(kind),
                      "experimentStatus": report["experimentStatus"], "preparedAt": public_time()}
            _store(repository, query, {source_key: source})
            return source
        if payload.get("action") != "save":
            raise ContractError("不支持的报告操作。")
        text, model = payload.get("text"), payload.get("model")
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ContractError("报告解读内容为空或过长。")
        if kind == "conclusion":
            _validate_conclusion(text.strip())
        if not isinstance(model, str) or not model.strip() or len(model) > 160:
            raise ContractError("缺少有效的解读模型标识。")
        row = repository.get_report(user_id, workspace_id, experiment_id) or {}
        source, previous = row.get(source_key), row.get(result_key)
        original = next((candidate for candidate in (source, previous) if candidate
                         and candidate["sourceId"] == payload.get("sourceId")), None)
        if original and original.get("analysis"):
            text = _format_analysis_text(text, kind)
            if not text:
                raise ContractError("报告解读内容为空或过长。")
        if previous and previous["sourceId"] == payload.get("sourceId"):
            if previous["text"] == text.strip() and previous["model"] == model.strip():
                return previous
            raise ContractError("此报告已保存，未覆盖原解读。")
        if not source or source["sourceId"] != payload.get("sourceId") or source["executionVersion"] != version:
            raise ContractError("报告来源已更新或清除，请重新生成解读。")
        if source.get("analysis"):
            evidence_ids = {item["id"] for item in source["analysis"]["evidence"]}
            cited = set(re.findall(r"\[(E\d+)\]", text))
            if cited - evidence_ids:
                raise ContractError("解读引用了不存在的分析证据，请重新生成。")
            found_sections = tuple(re.findall("|".join(re.escape(label) for label in REPORT_SECTIONS), text))
            if kind == "interpretation" and (not cited or found_sections != REPORT_SECTIONS or not text.startswith(REPORT_SECTIONS[0])):
                raise ContractError("报告解读缺少规定的分析段落或证据引用，请重新生成。")
        saved = {**source, "text": text.strip(), "model": model.strip(), "generatedAt": public_time()}
        _store(repository, query, {result_key: saved, source_key: None})
        return saved
