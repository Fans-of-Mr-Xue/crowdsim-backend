"""Code-only graceful runtime close after a run settles: fake transport, no simulator.

The orchestrator must release only the simulator bound to the active run, after
its final frame/archive are persisted. A mismatched or missing close
acknowledgement is logged, never retried against an unbound runtime, and an
explicitly paused / cancelled / restart-pending run stays attached.
"""
from copy import deepcopy
import threading

import pytest

from crowdsim.experiments.controller_registry import ControllerRegistry
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.repository import CrowdSimRepository


class Gateway:
    def __init__(self):
        self.commands = []
        self.closed_run_id = None

    def add_listener(self, callback):
        self.callback = callback

    def start(self, **kwargs):
        return True

    def submit(self, payload):
        self.commands.append(deepcopy(payload))
        return len(self.commands) - 1

    def wait_for_ack(self, request_id, **kwargs):
        command = self.commands[request_id]
        if command["action"] == "close_run":
            return {"type": "run_closed", "run_id": self.closed_run_id, "runtime_state": "CLOSED"}
        return {"type": "command_result", "status": "applied"}

    def forget_request(self, request_id):
        pass

    def close(self):
        pass

    def health(self):
        return {"connected": True}


def _setup(status="completed", runtime_run_id="rt-a", restart_pending=False):
    repo = CrowdSimRepository()
    gateway = Gateway()
    orchestrator = ExperimentOrchestrator(repo, gateway, ControllerRegistry())
    experiment = repo.create_experiment(
        "u", "w", {"name": "close check", "scenario": {"durationSeconds": 10}})
    run = repo.create_run("u", "w", experiment["experiment_id"], "C0", 1, {})
    repo.update_run("u", "w", run["run_id"], {
        "status": status, "runtime_run_id": runtime_run_id,
        "restartPending": restart_pending,
    })
    run = repo.get_run("u", "w", run["run_id"])
    active = {
        "user_id": "u", "workspace_id": "w", "experiment": experiment, "run": run,
        "controller": None, "builder": None, "terminal": threading.Event(),
        "failure": None, "runtime_run_id": runtime_run_id,
    }
    return repo, gateway, orchestrator, experiment, run, active


def _close_commands(gateway):
    return [command for command in gateway.commands if command["action"] == "close_run"]


def test_completed_run_closes_only_its_own_runtime_and_keeps_archive():
    repo, gateway, orchestrator, experiment, run, active = _setup("completed", "rt-a")
    gateway.closed_run_id = "rt-a"
    repo.record("observations", "u", "w", run["run_id"], {"simTimeSeconds": 10})

    orchestrator._graceful_close_owned_runtime(active, reason="completed")

    close_commands = _close_commands(gateway)
    assert len(close_commands) == 1
    assert close_commands[0]["run_id"] == "rt-a"
    # The archived observation history is retained; closing the runtime must
    # not clear the run's saved frame/records.
    assert repo.list_records("observations", "u", "w", run["run_id"])


def test_mismatched_close_ack_is_not_retried_against_another_runtime():
    repo, gateway, orchestrator, experiment, run, active = _setup("completed", "rt-a")
    gateway.closed_run_id = "rt-b"  # another attached runtime

    orchestrator._graceful_close_owned_runtime(active, reason="completed")

    close_commands = _close_commands(gateway)
    assert len(close_commands) == 1
    # The close stays addressed to the owned runtime; no unbound second close.
    assert all(command.get("run_id") == "rt-a" for command in close_commands)


@pytest.mark.parametrize("status,pending", [
    ("paused", False), ("cancelled", False), ("completed", True),
])
def test_paused_cancelled_or_pending_run_stays_attached(status, pending):
    repo, gateway, orchestrator, experiment, run, active = _setup(status, "rt-a", pending)

    orchestrator._graceful_close_owned_runtime(active, reason="completed")

    assert _close_commands(gateway) == []


def test_failed_worker_closes_the_owned_runtime(monkeypatch):
    repo, gateway, orchestrator, experiment, run, active = _setup("failed", "rt-a")
    gateway.closed_run_id = "rt-a"
    item = {"user_id": "u", "workspace_id": "w", "experiment": experiment, "run": run}

    def execute(_item):
        orchestrator._active = active
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "_execute_run", execute)
    orchestrator._queue.put(item)
    orchestrator._queue.put(None)
    orchestrator._worker_loop()

    close_commands = _close_commands(gateway)
    assert len(close_commands) == 1
    assert close_commands[0]["run_id"] == "rt-a"
    assert repo.get_run("u", "w", run["run_id"])["status"] == "failed"


def test_failure_does_not_close_a_runtime_bound_to_another_run(monkeypatch):
    repo, gateway, orchestrator, experiment, run, active = _setup("failed", "rt-a")
    other = repo.create_run("u", "w", experiment["experiment_id"], "C0", 2, {})
    other_active = {**active, "run": other, "runtime_run_id": "rt-other"}
    gateway.closed_run_id = "rt-other"
    item = {"user_id": "u", "workspace_id": "w", "experiment": experiment, "run": run}

    def execute(_item):
        orchestrator._active = other_active
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "_execute_run", execute)
    orchestrator._queue.put(item)
    orchestrator._queue.put(None)
    orchestrator._worker_loop()

    assert _close_commands(gateway) == []
