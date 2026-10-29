"""Code-only round restart checks: fake transport/worker, no simulator or HTTP server."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from crowdsim.experiments.contracts import ContractError
from crowdsim.experiments.controller_registry import ControllerRegistry
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.serve import ControlRequestHandler, CONTROL_PREFIX
from crowdsim.experiments.during_observation_restart import current_observation_run
from crowdsim.experiments.during_observation_reset import runtime_paths


@pytest.fixture
def study():
    return {"name": "本轮重跑代码检查", "duringObservation": {"conversationId": "check", "snapshotId": "snapshot"},
            "methods": ["C0"], "scenario": {"seedSet": [1, 2, 3]}, "evaluationWindowSeconds": 30}


class Gateway:
    latest_frame = None
    def __init__(self):
        self.commands = []
    def add_listener(self, callback):
        self.callback = callback
    def submit(self, payload):
        self.commands.append(deepcopy(payload))
        return len(self.commands) - 1
    def start(self, **kwargs):
        return True
    def wait_for_ack(self, request_id, **kwargs):
        if self.commands[request_id]["action"] == "get_status":
            return {"type": "status", "state": "PAUSED", "run_id": "external-run"}
        assert self.commands[request_id]["action"] == "close_run"
        return {"type": "run_closed"}
    def close(self):
        pass


def request(bundle, path, payload, workspace):
    handler = object.__new__(ControlRequestHandler)
    handler.server = SimpleNamespace(bundle=bundle)
    body = json.dumps(payload).encode()
    handler.path = CONTROL_PREFIX + path
    handler.headers = {"X-CrowdSim-User": "u", "X-CrowdSim-Workspace": workspace, "Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    result = []
    handler._send_json = lambda status, data, **kw: result.append((status, data, kw))
    handler.do_POST()
    return result[0]


def batch(study, monkeypatch):
    repo, gateway = CrowdSimRepository(), Gateway()
    orchestrator = ExperimentOrchestrator(repo, gateway, ControllerRegistry())
    monkeypatch.setattr(orchestrator, "_ensure_worker", lambda: None)
    monkeypatch.setattr("crowdsim.experiments.orchestrator.runtime_provenance", lambda: {})
    experiment = repo.create_experiment("u", "during-check", study)
    runs = orchestrator.start_experiment("u", "during-check", experiment["experiment_id"])
    return repo, gateway, orchestrator, experiment, runs


def restart(orchestrator, run, count=0):
    return orchestrator.restart_observation_run("u", "during-check", run["run_id"], {"expectedRestartCount": count})


def test_active_round_restarts_before_next_round_and_preserves_previous_evidence(study, monkeypatch):
    repo, gateway, orchestrator, experiment, runs = batch(study, monkeypatch)
    repo.update_run("u", "during-check", runs[0]["run_id"], {"status": "completed", "summary": {"keep": 10}})
    first = repo.get_run("u", "during-check", runs[0]["run_id"])
    for run in runs[:2]:
        repo.record("observations", "u", "during-check", run["run_id"], {"old": True})
    orchestrator._queue.get_nowait(); orchestrator._queue.task_done()  # Already completed round 1.
    attempts = []
    def execute(item):
        run = item["run"]
        attempts.append((run["seed"], run.get("restartCount", 0)))
        if run["run_id"] == runs[1]["run_id"] and not run.get("restartCount"):
            repo.update_run("u", "during-check", run["run_id"], {"status": "paused"})
            active = {**item, "terminal": threading.Event(), "runtime_run_id": "old-runtime"}
            orchestrator._active = active
            accepted = restart(orchestrator, run)
            assert accepted["runs"][1]["restartPending"]
            assert active["terminal"].is_set() and not active["accept_frames"]
            assert repo.list_records("observations", "u", "during-check", run["run_id"])
            restart(orchestrator, run)  # Lost-response retry while the old worker unwinds.
            assert repo.get_run("u", "during-check", run["run_id"])["restartCount"] == 1
            raise RuntimeError("old interrupted attempt")
        assert not repo.list_records("observations", "u", "during-check", run["run_id"])
        repo.record("observations", "u", "during-check", run["run_id"], {"new": True})
        repo.update_run("u", "during-check", run["run_id"], {"status": "completed"})
    monkeypatch.setattr(orchestrator, "_execute_run", execute)
    orchestrator._queue.put(None)
    orchestrator._worker_loop()
    assert attempts == [(2, 0), (2, 1), (3, 0)]
    assert repo.get_run("u", "during-check", runs[0]["run_id"]) == first
    assert repo.list_records("observations", "u", "during-check", runs[0]["run_id"])[0]["payload"] == {"old": True}
    assert repo.list_records("observations", "u", "during-check", runs[1]["run_id"])[0]["payload"] == {"new": True}
    assert orchestrator._queue.unfinished_tasks == 0
    assert repo.get_experiment("u", "during-check", experiment["experiment_id"])["status"] == "completed"


@pytest.mark.parametrize("status", ["running", "paused", "preparing", "failed", "cancelled"])
def test_recover_interrupted_control_service_without_rerunning_completed_rounds(study, monkeypatch, status):
    repo, gateway, old, experiment, runs = batch(study, monkeypatch)
    repo.update_run("u", "during-check", runs[0]["run_id"], {"status": "completed", "summary": {"keep": 1}})
    repo.update_run("u", "during-check", runs[1]["run_id"], {"status": status, "runtime_run_id": "external-run"})
    # New orchestrator deliberately has no in-memory active task or queue.
    restored = ExperimentOrchestrator(repo, gateway, ControllerRegistry())
    monkeypatch.setattr(restored, "_ensure_worker", lambda: None)
    result = restart(restored, runs[1])
    assert [row["run_id"] for row in result["runs"]] == [run["run_id"] for run in runs]
    assert result["runs"][0]["summary"] == {"keep": 1}
    assert [item["run"]["seed"] for item in restored._queue.queue] == [2, 3]
    restart(restored, runs[1])
    assert restored._queue.qsize() == 2
    assert repo.get_run("u", "during-check", runs[1]["run_id"])["restartCount"] == 1
    # Its prior runtime is still recognized as owned, even when left paused.
    def controller_boundary(*args):
        raise RuntimeError("code-only controller boundary reached")
    monkeypatch.setattr(restored.registry, "create", controller_boundary)
    with pytest.raises(RuntimeError, match="controller boundary reached"):
        restored._execute_run(restored._queue.get_nowait())
    assert any(command["action"] == "close_run" for command in gateway.commands)


def test_round_cleanup_is_scoped_and_invalidates_only_its_group_report(study, monkeypatch):
    repo, gateway, orchestrator, experiment, runs = batch(study, monkeypatch)
    for run in runs:
        repo.update_run("u", "during-check", run["run_id"], {"status": "completed", "summary": {"old": True}, "completion": {"old": True}})
        for kind in repo.APPEND_ONLY:
            repo.record(kind, "u", "during-check", run["run_id"], {"old": True})
    before = repo.list_runs("u", "during-check", experiment["experiment_id"])
    repo.save_report("u", "during-check", experiment["experiment_id"], {"old": True})
    repo.save_report("other", "foreign", "foreign-experiment", {"keep": True})
    status, result, _ = request(SimpleNamespace(orchestrator=orchestrator),
        f"/runs/{runs[2]['run_id']}/restart-observation", {"expectedRestartCount": 0}, workspace="during-check")
    assert status == 202 and result["status"] == "running"
    after = repo.list_runs("u", "during-check", experiment["experiment_id"])
    assert after[:2] == before[:2]
    assert after[2]["seed"] == before[2]["seed"] and after[2]["config"] == before[2]["config"]
    assert "summary" not in after[2] and "completion" not in after[2]
    for kind in repo.APPEND_ONLY:
        assert len(repo.list_records(kind, "u", "during-check", runs[0]["run_id"])) == 1
        assert repo.list_records(kind, "u", "during-check", runs[2]["run_id"]) == []
    assert repo.get_report("u", "during-check", experiment["experiment_id"]) is None
    assert repo.get_report("other", "foreign", "foreign-experiment")["report"] == {"keep": True}
    assert not gateway.commands


def test_restart_rejects_foreign_phases_queued_or_stale_rounds_and_versions(study, monkeypatch):
    repo, gateway, orchestrator, experiment, runs = batch(study, monkeypatch)
    with pytest.raises(ContractError, match="轮次已变化"):
        restart(orchestrator, runs[0])  # Nothing has started yet.
    repo.update_run("u", "during-check", runs[0]["run_id"], {"status": "completed"})
    repo.update_run("u", "during-check", runs[1]["run_id"], {"status": "running"})
    with pytest.raises(ContractError, match="轮次已变化"):
        restart(orchestrator, runs[0])
    with pytest.raises(LookupError):
        orchestrator.restart_observation_run("other", "during-check", runs[1]["run_id"], {"expectedRestartCount": 0})
    for count in (None, -1, True, "0", 3):
        with pytest.raises(ContractError):
            restart(orchestrator, runs[1], count)
    repo.update_experiment("u", "during-check", experiment["experiment_id"], {"duringObservation": None})
    with pytest.raises(ContractError, match="仅用于事中"):
        restart(orchestrator, runs[1])
    assert not gateway.commands


def test_ending_group_cancels_pending_restart(study, monkeypatch):
    repo, gateway, orchestrator, experiment, runs = batch(study, monkeypatch)
    item = orchestrator._queue.get_nowait()
    orchestrator._processing = item
    orchestrator._active = {**item, "terminal": threading.Event()}
    repo.update_run("u", "during-check", runs[0]["run_id"], {"status": "running"})
    restart(orchestrator, runs[0])
    orchestrator.stop_experiment("u", "during-check", experiment["experiment_id"])
    assert not repo.get_run("u", "during-check", runs[0]["run_id"])["restartPending"]
    assert all(run["status"] == "cancelled" for run in repo.list_runs("u", "during-check", experiment["experiment_id"]))


def test_cancelled_unstarted_rounds_are_not_selected_as_current():
    runs = [{"run_id": "r1", "status": "completed"}, {"run_id": "r2", "status": "cancelled", "started_at": "started"},
            {"run_id": "r3", "status": "cancelled", "started_at": None}]
    assert current_observation_run(runs)["run_id"] == "r2"
    assert current_observation_run([runs[-1]]) is None


def test_queued_retry_survives_control_service_exit_without_incrementing_attempt(study, monkeypatch):
    repo, gateway, orchestrator, experiment, runs = batch(study, monkeypatch)
    repo.update_run("u", "during-check", runs[0]["run_id"], {"status": "completed"})
    repo.update_run("u", "during-check", runs[1]["run_id"], {"status": "paused"})
    restart(orchestrator, runs[1])
    restored = ExperimentOrchestrator(repo, gateway, ControllerRegistry())
    monkeypatch.setattr(restored, "_ensure_worker", lambda: None)
    result = restart(restored, runs[1])  # Same persisted count/receipt, no in-memory queue.
    assert result["runs"][1]["restartCount"] == 1
    assert [item["run"]["seed"] for item in restored._queue.queue] == [2, 3]
    restart(restored, runs[1])
    assert restored._queue.qsize() == 2


def test_group_reset_can_clean_previous_attempt_directories_with_existing_path_checks(monkeypatch, tmp_path):
    monkeypatch.setattr("crowdsim.experiments.during_observation_reset.RUNTIME_ROOT", tmp_path)
    current, previous = "run-" + "a" * 32, "run-" + "b" * 32
    runs = [{"runtime_run_id": current, "previous_runtime_run_ids": [previous]}]
    assert {path.name for path in runtime_paths(runs, [])} == {current, previous}
    with pytest.raises(ContractError, match="其他实验引用"):
        runtime_paths(runs, [{"runtime_run_id": previous}])
    with pytest.raises(ContractError, match="编号无效"):
        runtime_paths([{**runs[0], "previous_runtime_run_ids": ["../outside"]}], [])
    # Exercise the symlink refusal without requiring Windows symlink privileges.
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == previous)
    with pytest.raises(ContractError, match="清理范围"):
        runtime_paths(runs, [])


@pytest.mark.parametrize("collection", ["reports", "runs", "experiments"])
def test_pending_restart_can_recover_partial_writes_after_reload(study, monkeypatch, tmp_path, collection):
    repo, gateway, old, experiment, runs = batch(study, monkeypatch)
    persistent = CrowdSimRepository(tmp_path / "control")
    persistent._memory = deepcopy(repo._memory)
    for kind in persistent.COLLECTIONS:
        persistent._flush(kind)
    restored = ExperimentOrchestrator(persistent, gateway, ControllerRegistry())
    monkeypatch.setattr(restored, "_ensure_worker", lambda: None)
    persistent.update_run("u", "during-check", runs[0]["run_id"], {"status": "paused"})
    flush, writes = persistent._flush, []
    def fail_once(kind):
        # Let the pending restart marker persist before failing cleanup.
        if kind == collection:
            writes.append(kind)
            if len(writes) == (2 if collection == "runs" else 1):
                raise OSError("synthetic restart persistence failure")
        return flush(kind)
    monkeypatch.setattr(persistent, "_flush", fail_once)
    with pytest.raises(OSError, match="persistence failure"):
        restart(restored, runs[0])
    loaded = CrowdSimRepository(tmp_path / "control")
    recovered = ExperimentOrchestrator(loaded, gateway, ControllerRegistry())
    monkeypatch.setattr(recovered, "_ensure_worker", lambda: None)
    result = restart(recovered, runs[0])
    assert result["status"] == "running" and result["runs"][0]["restartCount"] == 1
    assert [item["run"]["seed"] for item in recovered._queue.queue] == [1, 2, 3]
