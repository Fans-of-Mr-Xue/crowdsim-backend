"""Current-round speed checks using in-memory storage and a fake gateway only."""
from copy import deepcopy
from io import BytesIO
import json
import threading
from types import SimpleNamespace

import pytest

from crowdsim.experiments.contracts import ContractError
from crowdsim.experiments.controller_registry import ControllerRegistry
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.serve import CONTROL_PREFIX, ControlRequestHandler


class Gateway:
    capabilities = {}

    def __init__(self):
        self.commands, self.forgotten = [], []
        self.reply = {"type": "speed", "speedFactor": 2}
        self.before_ack = lambda: None

    def add_listener(self, callback):
        self.callback = callback

    def submit(self, payload):
        self.commands.append(deepcopy(payload))
        return str(len(self.commands) - 1)

    def wait_for_ack(self, request_id, timeout):
        # Prove frame callbacks can still take the lock during the HTTP wait.
        done = threading.Event()
        def frame_callback():
            with self.orchestrator._lock:
                done.set()
        thread = threading.Thread(target=frame_callback, daemon=True)
        thread.start()
        assert done.wait(1), "speed ACK wait must not hold the frame lock"
        thread.join(1)
        self.before_ack()
        return deepcopy(self.reply)

    def forget_request(self, request_id):
        self.forgotten.append(request_id)


@pytest.fixture
def active_round():
    repo, gateway = CrowdSimRepository(), Gateway()
    orchestrator = ExperimentOrchestrator(repo, gateway, ControllerRegistry())
    gateway.orchestrator = orchestrator
    experiment = repo.create_experiment("u", "during-check", {
        "name": "速度检查", "duringObservation": {"conversationId": "check", "snapshotId": "snapshot"},
        "scenario": {"seedSet": [1, 2]}, "methods": ["C0"], "evaluationWindowSeconds": 30,
    })
    run = repo.create_run("u", "during-check", experiment["experiment_id"], "C0", 1, {}, formal=True)
    run = repo.update_run("u", "during-check", run["run_id"], {"status": "paused", "runtime_run_id": "runtime-one"})
    active = {"user_id": "u", "workspace_id": "during-check", "experiment": experiment, "run": run,
              "runtime_run_id": "runtime-one", "terminal": threading.Event(), "accept_frames": True}
    orchestrator._active = active
    orchestrator._runtime_view = {"scope": ("u", "during-check", experiment["experiment_id"]), "runId": run["run_id"],
                                  "init": {"run_id": "runtime-one"}, "frame": {"step_seconds": 25}, "speedFactor": 3}
    return repo, gateway, orchestrator, experiment, run


def change(parts, **payload):
    return parts[2].set_run_speed("u", "during-check", parts[4]["run_id"],
                                 {"speedFactor": 2, "expectedRuntimeRunId": "runtime-one", **payload})


@pytest.mark.parametrize("status", ["running", "paused"])
@pytest.mark.parametrize("speed", [0.05, 2, 3])
def test_speed_ack_preserves_state_time_frozen_configuration_and_formal_flag(active_round, status, speed):
    repo, gateway, orchestrator, experiment, run = active_round
    repo.update_run("u", "during-check", run["run_id"], {"status": status})
    original_run = repo.get_run("u", "during-check", run["run_id"])
    gateway.reply["speedFactor"] = speed
    assert change(active_round, speedFactor=speed) == {"runId": run["run_id"], "runtimeRunId": "runtime-one", "speedFactor": speed}
    assert gateway.commands == [{"action": "set_speed", "run_id": "runtime-one", "speedFactor": speed}]
    assert gateway.forgotten == ["0"]
    view = orchestrator.runtime_view("u", "during-check", experiment["experiment_id"])
    assert view["speedFactor"] == speed and view["frame"]["step_seconds"] == 25
    assert repo.get_run("u", "during-check", run["run_id"]) == original_run
    assert repo.get_experiment("u", "during-check", experiment["experiment_id"]) == experiment
    assert "speedFactor" not in orchestrator.runtime_view("u", "during-check", experiment["experiment_id"], summary_only=True)


@pytest.mark.parametrize("value", [True, False, None, "2", [], {}, 0, -1, 0.049, 3.01, float("nan"), float("inf"), -float("inf"), 10**1000])
def test_invalid_speed_never_dispatches(active_round, value):
    with pytest.raises(ContractError):
        change(active_round, speedFactor=value)
    assert not active_round[1].commands


@pytest.mark.parametrize("value", [None, "", 12, "previous-attempt"])
def test_missing_or_stale_runtime_never_dispatches(active_round, value):
    with pytest.raises((ContractError, RuntimeError)):
        change(active_round, expectedRuntimeRunId=value)
    assert not active_round[1].commands


@pytest.mark.parametrize("status", ["draft", "queued", "preparing", "completed", "failed", "cancelled"])
def test_inactive_round_never_dispatches(active_round, status):
    repo, gateway, _, _, run = active_round
    repo.update_run("u", "during-check", run["run_id"], {"status": status})
    with pytest.raises(RuntimeError, match="RUN_NOT_ACTIVE"):
        change(active_round)
    assert not gateway.commands


@pytest.mark.parametrize("condition", ["foreign-user", "foreign-workspace", "restart", "terminal", "not-ready", "stopped", "other-class", "row-runtime"])
def test_scope_and_attempt_guards(active_round, condition):
    repo, gateway, orchestrator, experiment, run = active_round
    if condition.startswith("foreign-"):
        with pytest.raises(LookupError):
            orchestrator.set_run_speed("other" if condition == "foreign-user" else "u",
                                       "other" if condition == "foreign-workspace" else "during-check", run["run_id"],
                                       {"speedFactor": 2, "expectedRuntimeRunId": "runtime-one"})
    else:
        if condition == "restart":
            repo.update_run("u", "during-check", run["run_id"], {"restartPending": True})
        elif condition == "terminal":
            orchestrator._active["terminal"].set()
        elif condition == "not-ready":
            orchestrator._active["accept_frames"] = False
        elif condition == "stopped":
            repo.update_experiment("u", "during-check", experiment["experiment_id"], {"stop_requested": True})
        elif condition == "other-class":
            repo.update_experiment("u", "during-check", experiment["experiment_id"], {"duringObservation": None})
        else:
            repo.update_run("u", "during-check", run["run_id"], {"runtime_run_id": "other"})
        with pytest.raises((RuntimeError, ContractError)):
            change(active_round)
    assert not gateway.commands


@pytest.mark.parametrize("reply", [None, {}, {"type": "error", "message": "rejected"}, {"type": "command_result", "speedFactor": 2},
                                  {"type": "speed", "speedFactor": True}, {"type": "speed", "speedFactor": "2"},
                                  {"type": "speed", "speedFactor": float("nan")}, {"type": "speed", "speedFactor": 3.01}])
def test_failed_ack_never_claims_success_and_cleans_pending_request(active_round, reply):
    active_round[1].reply = reply
    with pytest.raises((RuntimeError, TimeoutError)):
        change(active_round)
    assert active_round[2]._runtime_view["speedFactor"] == 3
    assert active_round[1].forgotten == ["0"]


@pytest.mark.parametrize("condition", ["terminal", "replaced", "runtime"])
def test_late_ack_cannot_update_a_finished_or_replaced_attempt(active_round, condition):
    _, gateway, orchestrator, _, _ = active_round
    def handover():
        if condition == "terminal":
            orchestrator._active["terminal"].set()
        elif condition == "replaced":
            orchestrator._active = dict(orchestrator._active)
        else:
            orchestrator._active["runtime_run_id"] = "next-attempt"
    gateway.before_ack = handover
    with pytest.raises(RuntimeError, match="RUN_NOT_ACTIVE"):
        change(active_round)
    assert orchestrator._runtime_view["speedFactor"] == 3


def test_http_speed_endpoint_scopes_and_returns_only_confirmed_multiplier(active_round):
    _, _, orchestrator, _, run = active_round
    handler = object.__new__(ControlRequestHandler)
    handler.server = SimpleNamespace(bundle=SimpleNamespace(orchestrator=orchestrator))
    body = json.dumps({"speedFactor": 2, "expectedRuntimeRunId": "runtime-one"}).encode()
    handler.path = f"{CONTROL_PREFIX}/runs/{run['run_id']}/speed"
    handler.headers = {"X-CrowdSim-User": "u", "X-CrowdSim-Workspace": "during-check", "Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    response = []
    handler._send_json = lambda *args, **kwargs: response.append((args, kwargs))
    handler.do_POST()
    assert response == [((200, {"runId": run["run_id"], "runtimeRunId": "runtime-one", "speedFactor": 2}), {})]
