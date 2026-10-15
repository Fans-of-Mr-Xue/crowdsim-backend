"""Class-2 code-only checks. Models, gateways and observations are synthetic."""
from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from crowdsim.experiments.contracts import ContractError, validate_experiment_config
from crowdsim.experiments.controller_registry import ControllerRegistry
from crowdsim.experiments.during_strategy import DuringStrategyService, validate_strategy, validate_replay_binding
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.report_service import ReportService
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.serve import ControlRequestHandler, CONTROL_PREFIX
from crowdsim.control.intervention_lifecycle import InterventionLifecycle
from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.domain.runtime_capabilities import control_capabilities
from crowdsim.environment.information_model import InformationModel
from crowdsim.environment.intervention_executor import InterventionExecutor
from test_during_experiments import config, final_evidence, observation, Gateway, active_run


STRATEGY = {"name": "代码检查策略", "rationale": "根据已完成观察提出限流", "expectedEffect": "待验证拥挤变化", "limitations": "可能增加外围排队",
            "actions": [{"entryId": "178411801#0", "startSeconds": 120, "endSeconds": 240, "rate": .45}]}
GENERATION = {"method": "llm", "model": "synthetic-code-check", "request": "生成限流策略", "rawResponse": json.dumps(STRATEGY)}


def setup_source(root=None, statuses=("completed", "completed", "cancelled")):
    repo = CrowdSimRepository(root)
    exp = repo.create_experiment("u", "w", config(seedSet=list(range(1, len(statuses)+1))))
    for seed, status in enumerate(statuses, 1):
        run = repo.create_run("u", "w", exp["experiment_id"], "C0", seed, {})
        evidence = final_evidence()
        evidence.update(completion_rate=.2, t95_from_start_seconds=None)
        repo.record("observations", "u", "w", run["run_id"], observation(evidence))
        repo.update_run("u", "w", run["run_id"], {"status": status})
    repo.update_experiment("u", "w", exp["experiment_id"], {"status": "completed_with_failures"})
    return repo, DuringStrategyService(repo, ReportService(repo)), exp["experiment_id"]


def proposal(service, source):
    context = service.context("u", "w", source)
    payload = {"sourceFingerprint": context["sourceFingerprint"], "strategy": deepcopy(STRATEGY), "generation": deepcopy(GENERATION)}
    preview = service.preview("u", "w", source, payload)
    return {**payload, "confirmedFingerprint": preview["fingerprint"], "clientRequestId": "code-check-replay"}


def test_context_excludes_invalid_rounds_and_keeps_real_evidence():
    repo, service, source = setup_source()
    c = service.context("u", "w", source)
    assert [r["seed"] for r in c["runs"]] == [1, 2]
    assert c["runs"][0]["finalCompletionRate"] == .2
    assert c["runs"][0]["t95Seconds"] is None
    assert c["excludedRuns"][0]["seed"] == 3
    assert c["constraints"]["methods"] == {"llm": True, "xw": False}
    assert c["constraints"]["latestEndSeconds"] == 800
    assert c == service.context("u", "w", source)
    with pytest.raises(LookupError):
        service.context("other-user", "w", source)
    with pytest.raises(LookupError):
        service.context("u", "other-workspace", source)


@pytest.mark.parametrize("statuses", [("running",), ("draft",), ("paused",), ("failed",), ("cancelled",)])
def test_no_partial_pending_or_failed_source(statuses):
    _, service, source = setup_source(statuses=statuses)
    with pytest.raises(ContractError):
        service.context("u", "w", source)


@pytest.mark.parametrize("patch", [
    {"entryId": "unknown"}, {"startSeconds": 0}, {"startSeconds": 120.1}, {"endSeconds": 801},
    {"endSeconds": 120}, {"rate": -1}, {"rate": 1.1}, {"rate": True}, {"rate": float("nan")},
    {"rate": float("inf")}, {"actionType": "reroute_group"},
])
def test_strict_action_bounds(patch):
    strategy = deepcopy(STRATEGY)
    strategy["actions"][0].update(patch)
    with pytest.raises(ContractError):
        validate_strategy(strategy, config()["scenario"]["duringPreset"])


def test_overlap_and_unsupported_method_are_rejected():
    repo, service, source = setup_source()
    p = proposal(service, source)
    p["strategy"]["actions"] *= 2
    with pytest.raises(ContractError, match="重叠"):
        service.preview("u", "w", source, p)
    p = proposal(service, source)
    p["generation"]["method"] = "xw"
    with pytest.raises(ContractError, match="所选策略生成方法尚未接入"):
        service.preview("u", "w", source, p)


def test_replay_confirmation_binding_persistence_idempotency_and_queue(tmp_path, monkeypatch):
    repo, service, source = setup_source(tmp_path)
    p = proposal(service, source)
    with pytest.raises(ContractError, match="确认"):
        service.create_replay("u", "w", source, {**p, "confirmedFingerprint": "old"})
    child = service.create_replay("u", "w", source, p)
    assert child["methods"] == ["D2"] and child["scenario"]["seedSet"] == [1, 2]
    assert repo.list_runs("u", "w", child["experiment_id"]) == []
    assert child["duringReplay"]["sourceExperimentId"] == source
    assert "clarificationSnapshot" not in child  # Do not falsely label a controlled replay as C0.
    reloaded = CrowdSimRepository(tmp_path)
    service = DuringStrategyService(reloaded, ReportService(reloaded))
    assert service.create_replay("u", "w", source, p)["experiment_id"] == child["experiment_id"]
    with pytest.raises(ContractError):
        validate_experiment_config(child)  # Public generic API cannot bypass review.
    orchestrator = ExperimentOrchestrator(reloaded, Gateway(), ControllerRegistry())
    monkeypatch.setattr(orchestrator, "_ensure_worker", lambda: None)
    monkeypatch.setattr("crowdsim.experiments.orchestrator.runtime_provenance", lambda: {})
    runs = orchestrator.start_experiment("u", "w", child["experiment_id"])
    assert len(runs) == 2 and {r["seed"] for r in runs} == {1, 2}
    assert all(r["method_id"] == "D2" for r in runs)
    assert len(orchestrator.start_experiment("u", "w", child["experiment_id"])) == 2
    assert orchestrator._queue.qsize() == 2


def test_stale_evidence_and_tampered_replay_rejected():
    repo, service, source = setup_source()
    p = proposal(service, source)
    child = service.create_replay("u", "w", source, p)
    bad = deepcopy(child)
    bad["scenario"]["seedSet"] = [1, 3]
    with pytest.raises(ContractError):
        validate_replay_binding(bad)
    bad = deepcopy(child)
    bad["controllerConfig"]["D2"]["strategy"]["actions"][0]["rate"] = .1
    with pytest.raises(ContractError):
        validate_replay_binding(bad)
    run = repo.list_runs("u", "w", source)[0]
    repo.update_run("u", "w", run["run_id"], {"status": "failed"})
    with pytest.raises(ContractError, match="证据已改变"):
        service.create_replay("u", "w", source, p)


def test_replay_controller_dispatches_once_at_scheduled_frame_not_report_interval():
    orchestrator, active = active_run()
    active["run"]["method_id"] = "D2"
    controller = ControllerRegistry().create("D2", {"strategy": STRATEGY, "strategyFingerprint": "test"})
    controller.reset()
    active.update(controller=controller, last_observation_time=119.5)
    evidence = final_evidence()
    evidence.update(time_seconds=120, run_complete=False, finished=False)
    frame = {"run_id": "sim", "runtime_state": "RUNNING", "step_seconds": 120, "metrics": {"during": evidence}}
    orchestrator._handle_frame(active, frame)
    commands = orchestrator.gateway.commands
    assert len(commands) == 1 and commands[0]["action"] == "apply_action"
    assert commands[0]["controlAction"]["parameters"]["endTimeSeconds"] == 240
    orchestrator._handle_frame(active, frame)
    assert len(commands) == 1
    controller.reset()
    assert controller.due(120) and not controller.due(119.5)


def test_absolute_deadline_includes_queue_delay_and_releases_holds():
    assert "endTimeSeconds" in control_capabilities()["supportedActions"]["set_inflow_rate"]
    payload = {"actionId": "a", "actionType": "set_inflow_rate", "target": {"entryId": "178411801#0"},
               "parameters": {"rate": .45, "durationSeconds": 120, "endTimeSeconds": 800}}
    executor = InterventionExecutor(InformationModel())
    record = executor.apply_command("set_inflow_rate", payload, 799.5)
    assert record["expiresAt"] == 800  # Not 919.5, and no minimum-1-second extension.
    holds = []
    runtime = SimpleNamespace(interventions=executor, time_seconds=800,
        _control_speed_limits={}, _control_risk_weights={}, _inflow_meters={"a": object()}, _inflow_holds={"a": {"p"}},
        plan_executor=SimpleNamespace(set_inflow_hold=lambda *args: holds.append(args)))
    SimulationRuntime._expire_control_actions(runtime)
    assert holds == [("p", "a", False)] and runtime._inflow_holds == {} and runtime._inflow_meters == {}
    # The runtime rejects an expired command before any queue/meter mutation.
    with pytest.raises(ValueError, match="迟到"):
        SimulationRuntime.apply_control_action(SimpleNamespace(time_seconds=800), {"controlAction": payload})
    with pytest.raises(ValueError):
        InterventionLifecycle().activate(payload, 800, {})


def test_comparison_uses_matching_seeds_applied_acks_and_null_t95(tmp_path):
    repo, service, source = setup_source(tmp_path, ("completed", "completed", "completed"))
    child = service.create_replay("u", "w", source, proposal(service, source))
    for seed in [1, 2, 3]:
        run = repo.create_run("u", "w", child["experiment_id"], "D2", seed, child["controllerConfig"]["D2"])
        evidence = final_evidence()
        evidence.update(completion_rate=.4, t95_from_start_seconds=None)
        repo.record("observations", "u", "w", run["run_id"], observation(evidence))
        repo.update_run("u", "w", run["run_id"], {"status": "completed" if seed != 3 else "cancelled"})
        repo.record("decisions", "u", "w", run["run_id"], {"actions": [{"actionId": "a"}]})
        repo.record("acks", "u", "w", run["run_id"], {"actionId": "a", "status": "applied" if seed != 2 else "queued"})
    result = ReportService(CrowdSimRepository(tmp_path)).generate("u", "w", child["experiment_id"])["strategyComparison"]
    assert result["validPairCount"] == 1 and result["plannedPairCount"] == 3
    assert result["pairs"][0]["seed"] == 1
    metrics = {m["metric"]: m for m in result["metrics"]}
    assert metrics["finalCompletionRate"]["meanDifference"] == .2
    assert metrics["t95Seconds"]["pairCount"] == 0 and metrics["t95Seconds"]["meanDifference"] is None
    assert len(result["excludedRuns"]) == 2
    assert result["descriptiveOnly"] is True


def request(bundle, path, payload=None, user="u", workspace="w"):
    handler = object.__new__(ControlRequestHandler)
    handler.server = SimpleNamespace(bundle=bundle)
    body = json.dumps(payload).encode() if payload is not None else b""
    handler.path = CONTROL_PREFIX + path
    handler.headers = {"X-CrowdSim-User": user, "X-CrowdSim-Workspace": workspace, "Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    result = []
    handler._send_json = lambda status, data, **kw: result.append((status, data, kw))
    (handler.do_GET if payload is None else handler.do_POST)()
    return result[0]


def test_http_routes_in_memory_without_server_or_sockets():
    repo, service, source = setup_source()
    bundle = SimpleNamespace(during_strategies=service, repository=repo)
    status, context, _ = request(bundle, f"/experiments/{source}/strategy-context")
    assert status == 200 and context["sourceExperimentId"] == source
    assert request(bundle, f"/experiments/{source}/strategy-context", user="other")[0] == 404
    p = proposal(service, source)
    assert request(bundle, f"/experiments/{source}/strategy-preview", p)[0] == 200
    status, replay, _ = request(bundle, f"/experiments/{source}/strategy-replays", p)
    assert status == 201 and replay["methods"] == ["D2"]
    assert repo.list_runs("u", "w", replay["experiment_id"]) == []
