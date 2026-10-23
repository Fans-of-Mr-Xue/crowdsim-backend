"""Only temporary persistence, fake transports and in-process HTTP handlers."""
from types import SimpleNamespace

import pytest

from crowdsim.experiments.contracts import ContractError
from crowdsim.experiments.during_observation import observation_config, verify_initialization
from crowdsim.experiments.during_observation_reports import interpretation, write_interpretation
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.report_service import ReportService
from test_during_observation_experiments import study, sample
from test_during_observation_reset import reset_case, reset
from test_during_strategy import request


@pytest.fixture
def report_case(study, tmp_path):
    payload, requirement, model = study
    repo = CrowdSimRepository(tmp_path / "reports")
    config = observation_config(payload, "during-check")
    experiment = repo.create_experiment("u", "during-check", config)
    eid = experiment["experiment_id"]
    evidence = verify_initialization(config["duringObservation"], {"run_id": "runtime", "requirement": requirement, "artificial_society": model, "observation_geometry": {"area": 5}})
    for index, seed in enumerate(config["scenario"]["seedSet"]):
        run = repo.create_run("u", "during-check", eid, "C0", seed, {})
        repo.update_run("u", "during-check", run["run_id"], {"status": "completed" if index == 0 else "running" if index == 1 else "draft", "observationBinding": evidence})
        if index == 0:
            for time in (1, 31, 60):
                repo.record("observations", "u", "during-check", run["run_id"], {"observationMetrics": sample(time, density=4, speed=None)})
    repo.update_experiment("u", "during-check", eid, {"status": "running"})
    return SimpleNamespace(repository=repo, reports=ReportService(repo), eid=eid)


def write(case, payload):
    return write_interpretation(case.repository, case.reports, "u", "during-check", case.eid, payload)


def prepare(case, kind="interpretation"):
    return write(case, {"action": "prepare", "executionVersion": 0, "kind": kind})


def save(case, source, **changes):
    payload = {"action": "save", "executionVersion": 0, "kind": source.get("kind", "interpretation"), "sourceId": source["sourceId"], "text": "已有一轮有效结果，速度缺失。", "model": "GLM-5.3-Flash", **changes}
    if payload["kind"] == "interpretation" and payload.get("text"):
        payload["text"] = formatted_report(payload["text"])
    return write(case, payload)


def formatted_report(answer):
    return (f"问题回答：{answer}\n关键证据：一轮有效结果[E01]。\n关注重点：暂不判断拥堵。"
            "\n验证建议：延长观察后复核。\n分析局限：速度缺失，不能作因果判断。")


def test_interpretation_preserves_evidence_and_survives_reload_without_changing_runs(report_case):
    c = report_case
    before = c.repository.list_runs("u", "during-check", c.eid)
    assert interpretation(c.repository, "u", "during-check", c.eid) is None
    source = prepare(c)
    assert source["facts"]["validRunCount"] == 1
    assert source["facts"]["aggregate"]["global-speed"]["mean"] is None
    assert "series" not in source["facts"]["runs"][0]
    saved = save(c, source)
    assert saved["facts"] == source["facts"] and saved["reportVersion"] == source["reportVersion"]
    assert save(c, source) == saved  # Lost response retry is idempotent.
    restored = CrowdSimRepository(c.repository._root)
    assert interpretation(restored, "u", "during-check", c.eid) == saved
    assert restored.list_runs("u", "during-check", c.eid) == before
    # Regenerating a measured report cannot discard its saved interpretation.
    ReportService(restored).generate("u", "during-check", c.eid)
    assert interpretation(restored, "u", "during-check", c.eid) == saved


def test_running_report_can_save_its_original_source_after_new_results_arrive(report_case):
    c = report_case
    source = prepare(c)
    run = c.repository.list_runs("u", "during-check", c.eid)[1]
    c.repository.update_run("u", "during-check", run["run_id"], {"status": "failed", "error": "synthetic failure"})
    current = c.reports.generate("u", "during-check", c.eid)
    assert current["reportVersion"] != source["reportVersion"]
    saved = save(c, source)
    assert saved["reportVersion"] == source["reportVersion"]
    assert saved["facts"]["runs"][1]["status"] == "running"
    assert c.repository.get_report("u", "during-check", c.eid)["report"]["reportVersion"] == current["reportVersion"]


@pytest.mark.parametrize("kind", ["interpretation", "conclusion"])
def test_failed_save_preserves_the_previous_report_and_retry_remains_persistent(report_case, monkeypatch, kind):
    c = report_case
    previous = save(c, prepare(c, kind))
    source = prepare(c, kind)
    flush = c.repository._flush
    def fail(kind):
        if kind == "reports":
            raise OSError("synthetic disk failure")
        return flush(kind)
    monkeypatch.setattr(c.repository, "_flush", fail)
    with pytest.raises(OSError, match="synthetic disk failure"):
        save(c, source, text="new text")
    assert interpretation(c.repository, "u", "during-check", c.eid, kind=kind) == previous
    monkeypatch.setattr(c.repository, "_flush", flush)
    saved = save(c, source, text="new text")
    restored = CrowdSimRepository(c.repository._root)
    assert interpretation(restored, "u", "during-check", c.eid, kind=kind) == saved


def test_new_prepare_rejects_an_older_model_reply_and_preserves_saved_text(report_case):
    c = report_case
    original = save(c, prepare(c))
    older, newer = prepare(c), prepare(c)
    with pytest.raises(ContractError, match="已更新或清除"):
        save(c, older)
    assert interpretation(c.repository, "u", "during-check", c.eid) == original
    assert save(c, newer)["sourceId"] == newer["sourceId"]


def test_conclusion_and_general_interpretation_keep_independent_sources_and_saved_results(report_case):
    c = report_case
    general_source, conclusion_source = prepare(c), prepare(c, "conclusion")
    assert conclusion_source["kind"] == "conclusion"
    assert "concise-simulation-summary" in conclusion_source["facts"]["selectedMetricIds"]
    requirement = c.repository.get_experiment("u", "during-check", c.eid)["duringObservation"]["requirement"]
    assert conclusion_source["facts"]["researchQuestion"] == requirement["project"]["description"]
    with pytest.raises(ContractError, match="来源已更新"):
        save(c, general_source, kind="conclusion")
    general = save(c, general_source, text="原来的大模型解读。这里允许多句。")
    conclusion = save(c, conclusion_source, text="阶段性结果：已有一轮有效观测，速度数据不足以判断流动趋势。")
    assert interpretation(c.repository, "u", "during-check", c.eid) == general
    assert interpretation(c.repository, "u", "during-check", c.eid, kind="conclusion") == conclusion
    # A newer request of one kind cannot invalidate the other kind's pending source.
    next_conclusion = prepare(c, "conclusion")
    prepare(c)
    updated = save(c, next_conclusion, text="阶段性结果：现有观测暂不足以确认人群聚集趋势。")
    restored = CrowdSimRepository(c.repository._root)
    assert interpretation(restored, "u", "during-check", c.eid) == general
    assert interpretation(restored, "u", "during-check", c.eid, kind="conclusion") == updated
    c.reports.generate("u", "during-check", c.eid)
    assert interpretation(c.repository, "u", "during-check", c.eid, kind="conclusion") == updated
    status, result, _ = request(c, f"/experiments/{c.eid}/observation-conclusion", workspace="during-check")
    assert status == 200 and result == updated
    assert request(c, f"/experiments/{c.eid}/observation-conclusion", workspace="another")[0] == 404


def test_conclusion_http_prepare_and_save_do_not_write_the_general_interpretation(report_case):
    c = report_case
    general = save(c, prepare(c), text="原有实验报告解读。")
    endpoint = f"/experiments/{c.eid}/observation-conclusion"
    status, source, _ = request(c, endpoint, {"action": "prepare", "executionVersion": 0, "kind": "interpretation"}, workspace="during-check")
    assert status == 200 and source["kind"] == "conclusion"
    status, saved, _ = request(c, endpoint, {"action": "save", "executionVersion": 0, "sourceId": source["sourceId"], "text": "阶段性结果：现有观测仍不足以判断聚集趋势。", "model": "GLM"}, workspace="during-check")
    assert status == 200 and saved["sourceId"] == source["sourceId"]
    assert request(c, endpoint, workspace="during-check")[1] == saved
    assert interpretation(c.repository, "u", "during-check", c.eid) == general


@pytest.mark.parametrize("text", ["第一句。第二句。", "第一行\n第二行", "x" * 201, "Is density 1.2. Speed 0.", "结论？补充说明"])
def test_conclusion_rejects_multi_sentence_multiline_or_overlong_output_without_overwriting(report_case, text):
    c = report_case
    previous = save(c, prepare(c, "conclusion"))
    source = prepare(c, "conclusion")
    with pytest.raises(ContractError, match="单句、单行"):
        save(c, source, text=text)
    assert interpretation(c.repository, "u", "during-check", c.eid, kind="conclusion") == previous
    assert save(c, source, text="阶段性结果：观测均值为 1.2，但现有证据不足以判断趋势。")["kind"] == "conclusion"


def test_conclusion_requires_selection_in_frozen_experiment_and_valid_measured_results(report_case):
    c = report_case
    experiment = c.repository.get_experiment("u", "during-check", c.eid)
    binding = experiment["duringObservation"]
    binding["requirement"]["observation"]["metric_ids"].remove("concise-simulation-summary")
    c.repository.update_experiment("u", "during-check", c.eid, {"duringObservation": binding})
    with pytest.raises(ContractError, match="未选择"):
        prepare(c, "conclusion")
    assert request(c, f"/experiments/{c.eid}/observation-conclusion", workspace="during-check")[0] == 400
    assert prepare(c)["kind"] == "interpretation"
    binding["requirement"]["observation"]["metric_ids"].append("concise-simulation-summary")
    c.repository.update_experiment("u", "during-check", c.eid, {"duringObservation": binding})
    for run in c.repository.list_runs("u", "during-check", c.eid):
        c.repository.update_run("u", "during-check", run["run_id"], {"status": "failed"})
    with pytest.raises(ContractError, match="有效完成轮次"):
        prepare(c, "conclusion")
    with pytest.raises(ContractError, match="不支持"):
        prepare(c, "arbitrary-output")


@pytest.mark.parametrize("patch", [{"executionVersion": 1}, {"executionVersion": False}, {"text": ""}, {"text": "x" * 16001}, {"model": ""}, {"sourceId": "foreign"}, {"action": "start"}])
def test_malformed_or_stale_writes_do_not_replace_saved_interpretation(report_case, patch):
    c = report_case
    original = save(c, prepare(c))
    source = prepare(c)
    with pytest.raises(ContractError):
        save(c, source, **patch)
    assert interpretation(c.repository, "u", "during-check", c.eid) == original


def test_routes_are_scoped_to_during_and_do_not_create_an_experiment(report_case):
    c = report_case
    assert request(c, f"/experiments/{c.eid}/observation-interpretation", workspace="during-check")[1] is None
    status, source, _ = request(c, f"/experiments/{c.eid}/observation-interpretation", {"action": "prepare", "executionVersion": 0}, workspace="during-check")
    assert status == 200
    assert request(c, f"/experiments/{c.eid}/observation-interpretation", user="other", workspace="during-check")[0] == 404
    assert request(c, f"/experiments/{c.eid}/observation-interpretation", workspace="post-check")[0] == 404
    other = c.repository.create_experiment("u", "during-check", {"name": "legacy pre/post"})
    assert request(c, f"/experiments/{other['experiment_id']}/observation-interpretation", workspace="during-check")[0] == 400
    assert c.repository.get_experiment("u", "during-check", c.eid)["status"] == "running"
    assert source["facts"]["binding"]["conversationId"] == "check"


def test_prepare_requires_measured_valid_results(report_case):
    c = report_case
    for run in c.repository.list_runs("u", "during-check", c.eid):
        c.repository.update_run("u", "during-check", run["run_id"], {"status": "failed"})
    with pytest.raises(ContractError, match="有效完成轮次"):
        prepare(c)


def test_destructive_reset_removes_saved_and_pending_interpretations(reset_case):
    c = reset_case
    report = ReportService(c.repo).generate("u", "during-check", c.eid)
    query = {"user_id": "u", "workspace_id": "during-check", "experiment_id": c.eid}
    pending = {"sourceId": "pending", "executionVersion": 0, "facts": {"old": True}}
    c.repo._update("reports", query, {"interpretation": {**pending, "text": "old"}, "interpretationSource": pending,
                                      "conclusion": {**pending, "kind": "conclusion", "text": "old conclusion"}, "conclusionSource": pending})
    reset(c)
    assert interpretation(c.repo, "u", "during-check", c.eid) is None
    assert interpretation(c.repo, "u", "during-check", c.eid, kind="conclusion") is None
    with pytest.raises(ContractError, match="重新运行"):
        write_interpretation(c.repo, ReportService(c.repo), "u", "during-check", c.eid, {"action": "save", "executionVersion": 0, "sourceId": "pending", "text": "late", "model": "GLM"})
    with pytest.raises(ContractError, match="重新运行"):
        write_interpretation(c.repo, ReportService(c.repo), "u", "during-check", c.eid, {"kind": "conclusion", "action": "save", "executionVersion": 0, "sourceId": "pending", "text": "late", "model": "GLM"})
    fresh = ReportService(c.repo).generate("u", "during-check", c.eid)
    assert fresh["executionVersion"] == 1 and fresh["reportVersion"] != report["reportVersion"]
    assert not c.repo.get_report("u", "during-check", c.eid).get("interpretationSource")
    assert not c.repo.get_report("u", "during-check", c.eid).get("conclusionSource")


def test_summary_query_omits_visual_frames_without_acquiring_or_controlling_simulator(reset_case):
    c = reset_case
    c.orchestrator._runtime_view.update(runId=c.runs[-1]["run_id"], frame={"step_seconds": 55, "entities": ["large frame"]})
    bundle = SimpleNamespace(orchestrator=c.orchestrator)
    status, summary, _ = request(bundle, f"/experiments/{c.eid}/runtime?summary=1", workspace="during-check")
    assert status == 200 and summary["elapsedSeconds"] == 55
    assert summary["binding"] == {key: c.repo.get_experiment("u", "during-check", c.eid)["duringObservation"][key] for key in summary["binding"]}
    assert "frame" not in summary and "init" not in summary and not c.gateway.commands
    summary["runs"][0]["status"] = "forged"
    assert c.repo.get_run("u", "during-check", c.runs[0]["run_id"])["status"] == "completed"
    assert request(bundle, f"/experiments/{c.eid}/runtime?summary=1", workspace="foreign")[0] == 404
    assert not c.gateway.commands


def measured_records(case):
    run_id = case.repository.list_runs("u", "during-check", case.eid)[0]["run_id"]
    return [r["payload"]["observationMetrics"] for r in case.repository._memory["observations"] if r["run_id"] == run_id]


def add_spatial_evidence(case):
    for sample_row in measured_records(case):
        peak = sample_row["time_seconds"] == 31
        sample_row["cells"] = [
            {"cell_id": "a", "person_count": 8 if peak else 2, "area_m2": 2,
             "density_person_per_m2": 4 if peak else 1, "avg_speed_mps": None if peak else 1, "speed_sample_count": 0 if peak else 2},
            {"cell_id": "b", "person_count": 4 if peak else 1, "area_m2": 2,
             "density_person_per_m2": 2 if peak else 0.5, "avg_speed_mps": 0 if peak else 0.2, "speed_sample_count": 4 if peak else 1},
        ]
    run = case.repository.list_runs("u", "during-check", case.eid)[0]
    evidence = run["observationBinding"]
    evidence["geometry"] = {"density_area_basis": "clipped_grid_area", "area_m2": 4,
                            "cells": [{"cell_id": "a", "row": 0, "column": 0}, {"cell_id": "b", "row": 1, "column": 1}]}
    case.repository.update_run("u", "during-check", run["run_id"], {"observationBinding": evidence})


def test_prepare_executes_analysis_and_loads_the_actual_during_skill_for_both_outputs(report_case):
    c = report_case
    add_spatial_evidence(c)
    before_runs = c.repository.list_runs("u", "during-check", c.eid)
    general, concise = prepare(c), prepare(c, "conclusion")
    assert general["analysis"] == concise["analysis"]
    assert general["skill"]["name"] == "during_experiment_analysis"
    assert "同网格热点定位" in general["skill"]["instruction"]
    assert "问题回答：" in general["skill"]["instruction"]
    assert "一个单行句子" in concise["skill"]["instruction"]
    assert general["facts"]["researchQuestion"] == concise["facts"]["researchQuestion"]
    assert len(general["analysis"]["executedSteps"]) == 5
    evidence = general["analysis"]["evidence"]
    quality = next(e for e in evidence if e["kind"] == "data-quality")
    assert quality["validRuns"] == 1 and quality["partial"]
    assert "单轮" in quality["replicationAssessment"]
    local = next(e for e in evidence if e.get("metricId") == "local-density")
    assert local["maximumSample"]["timeSeconds"] == 31
    assert local["maximumSample"]["value"] == 4
    assert local["windowComparison"]["meanChange"] == 0
    assert "单调" in local["windowComparison"]["definition"]
    speed = next(e for e in evidence if e.get("metricId") == "global-speed")
    assert speed["minimumSample"] is None
    assert speed["windowComparison"]["firstMean"] is None
    hotspots = [e for e in evidence if e["kind"] == "spatial-hotspot"]
    assert hotspots[0]["cellId"] == "a"
    assert hotspots[0]["peakSample"]["personCount"] == 8
    assert hotspots[0]["peakSample"]["areaM2"] == 2
    assert hotspots[0]["peakSample"]["sameCellSpeedMps"] is None
    assert hotspots[1]["peakSample"]["sameCellSpeedMps"] == 0
    assert hotspots[0]["runsWhereCellIsMaximum"] == 1
    assert c.repository.list_runs("u", "during-check", c.eid) == before_runs


def test_window_analysis_uses_actual_execution_binding_not_unexecuted_requirement_times(report_case):
    c = report_case
    run = c.repository.list_runs("u", "during-check", c.eid)[0]
    evidence = run["observationBinding"]
    evidence["hotspotActivity"] = {"start_seconds": 60, "end_seconds": 90}
    c.repository.update_run("u", "during-check", run["run_id"], {"observationBinding": evidence})
    source = prepare(c)
    window = next(e for e in source["analysis"]["evidence"] if e["kind"] == "observation-window")["runs"][0]
    assert window["activityStartSeconds"] == window["lastSeconds"] == 60
    assert "未覆盖活动开始后" in window["assessment"]
    assert "不能据此否定" in window["assessment"]
    assert window["activitySource"] == "本轮已核验的活动执行绑定"


def test_invalid_runs_cannot_contaminate_spatial_or_temporal_analysis(report_case):
    c = report_case
    add_spatial_evidence(c)
    invalid = c.repository.list_runs("u", "during-check", c.eid)[1]
    c.repository.record("observations", "u", "during-check", invalid["run_id"], {
        "observationMetrics": {"time_seconds": 31, "global": {"density_person_per_m2": 999999},
                               "cells": [{"cell_id": "fake", "density_person_per_m2": 999999}]}})
    source = prepare(c)
    evidence = source["analysis"]["evidence"]
    assert all(e.get("cellId") != "fake" for e in evidence)
    assert next(e for e in evidence if e.get("metricId") == "global-density")["maximumSample"]["value"] == 4
    assert next(e for e in evidence if e["kind"] == "data-quality")["excluded"][0]["runId"] == invalid["run_id"]


def test_replication_is_counted_per_seed_and_not_per_time_sample(report_case):
    from copy import deepcopy
    c = report_case
    add_spatial_evidence(c)
    first, second, _ = c.repository.list_runs("u", "during-check", c.eid)
    c.repository.update_run("u", "during-check", second["run_id"], {"status": "completed", "observationBinding": first["observationBinding"]})
    for row in measured_records(c):
        other = deepcopy(row)
        for cell in other["cells"]:
            cell["cell_id"] = "b" if cell["cell_id"] == "a" else "a"
        c.repository.record("observations", "u", "during-check", second["run_id"], {"observationMetrics": other})
    source = prepare(c)
    hotspots = [e for e in source["analysis"]["evidence"] if e["kind"] == "spatial-hotspot"]
    assert source["facts"]["validRunCount"] == 2
    assert len(hotspots) == 2
    assert all(e["measuredRunCount"] == 2 and e["runsWhereCellIsMaximum"] == 1 for e in hotspots)


def test_regional_recurrence_deduplicates_tied_peak_cells_within_one_run(report_case):
    c = report_case
    add_spatial_evidence(c)
    measured_records(c)[1]["cells"][1]["density_person_per_m2"] = 4
    run = c.repository.list_runs("u", "during-check", c.eid)[0]
    evidence = run["observationBinding"]
    evidence["geometry"]["cells"] = [{"cell_id": "a", "row": 0, "column": 0},
                                      {"cell_id": "b", "row": 0, "column": 1},
                                      {"cell_id": "far", "row": 8, "column": 8}]
    c.repository.update_run("u", "during-check", run["run_id"], {"observationBinding": evidence})
    hotspots = [e for e in prepare(c)["analysis"]["evidence"] if e["kind"] == "spatial-hotspot"]
    assert len(hotspots) == 2 and hotspots[0]["region"] == hotspots[1]["region"]
    assert all(e["runsWhereCellIsMaximum"] == 1 and e["runsWhereRegionIsMaximum"] == 1 for e in hotspots)


def test_intermediate_spatial_changes_invalidate_evidence_even_when_scalar_statistics_stay_equal(report_case):
    c = report_case
    add_spatial_evidence(c)
    source = prepare(c)
    sample_row = measured_records(c)[1]
    sample_row["cells"][0]["cell_id"], sample_row["cells"][1]["cell_id"] = "b", "a"
    current = c.reports.generate("u", "during-check", c.eid)
    assert current["aggregate"] == source["facts"]["aggregate"]
    assert current["observationEvidenceVersion"] != source["facts"]["observationEvidenceVersion"]
    newer = prepare(c)
    assert newer["analysis"]["evidenceFingerprint"] != source["analysis"]["evidenceFingerprint"]


@pytest.mark.parametrize("text", ["这是对统计表的复述。", formatted_report("发现。") + "[E999]", "问题回答：只有一个段落[E01]。"])
def test_report_requires_the_analysis_format_and_existing_evidence_without_overwriting(report_case, text):
    c = report_case
    previous = save(c, prepare(c))
    source = prepare(c)
    with pytest.raises(ContractError, match="证据|分析段落"):
        write(c, {"action": "save", "executionVersion": 0, "sourceId": source["sourceId"], "text": text, "model": "GLM"})
    assert interpretation(c.repository, "u", "during-check", c.eid) == previous


def test_no_selected_spatial_metric_does_not_invent_a_hotspot(report_case):
    c = report_case
    experiment = c.repository.get_experiment("u", "during-check", c.eid)
    binding = experiment["duringObservation"]
    binding["requirement"]["observation"]["metric_ids"] = ["global-speed", "concise-simulation-summary"]
    c.repository.update_experiment("u", "during-check", c.eid, {"duringObservation": binding})
    source = prepare(c)
    assert not any(e["kind"] == "spatial-hotspot" for e in source["analysis"]["evidence"])
    assert any("不定位空间热点" in note for note in source["analysis"]["limitations"])


def test_output_formatting_keeps_existing_text_ui_and_save_retries_idempotent(report_case):
    c = report_case
    source = prepare(c)
    text = formatted_report("局部聚集，需进一步观测。").replace("\n", "")
    payload = {"action": "save", "executionVersion": 0, "sourceId": source["sourceId"], "text": text, "model": "GLM"}
    saved = write(c, payload)
    assert len(saved["text"].split("\n\n")) == 5
    assert write(c, payload) == saved
    concise = save(c, prepare(c, "conclusion"), text="问题回答：现有采样显示局部聚集，但观察窗口不足以评价活动全过程。")
    assert concise["text"].startswith("现有采样")
