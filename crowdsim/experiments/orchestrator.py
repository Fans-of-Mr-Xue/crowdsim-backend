"""Experiment lifecycle and closed-loop control orchestration."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import logging
import queue
import random
import threading
import time
import uuid
from typing import Any, Mapping

from .action_validator import ActionValidator
from .contracts import ContractError, validate_decision, validate_experiment_config
from .effect_evaluator import EffectEvaluator
from .gateway_events import EventBus
from .metrics import summarize_observations
from .observation_builder import ObservationBuilder
from .provenance import runtime_provenance


class ExperimentOrchestrator:
    def __init__(self, repository, gateway, controller_registry, *, event_bus: EventBus | None = None) -> None:
        self.repository = repository
        self.gateway = gateway
        self.registry = controller_registry
        self.events = event_bus or EventBus()
        self.validator = ActionValidator()
        self.evaluator = EffectEvaluator()
        self.logger = logging.getLogger("crowdsim.experiments.orchestrator")
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.RLock()
        self._active: dict[str, Any] | None = None
        self._request_actions: dict[str, dict[str, Any]] = {}
        self.owner_id = f"crowdsim_{uuid.uuid4().hex}"
        self.gateway.add_listener(self._on_gateway_event)

    def health(self) -> dict[str, Any]:
        with self._lock:
            active = self._active["run"]["run_id"] if self._active else None
        return {"orchestrator": "ready", "gateway": self.gateway.health(), "activeRunId": active, "queuedRuns": self._queue.qsize()}

    def create_experiment(self, user_id: str, workspace_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        config = validate_experiment_config(payload)
        return self.repository.create_experiment(user_id, workspace_id, config)

    def start_experiment(self, user_id: str, workspace_id: str, experiment_id: str) -> list[dict[str, Any]]:
        experiment = self.repository.get_experiment(user_id, workspace_id, experiment_id)
        if not experiment:
            raise LookupError("EXPERIMENT_NOT_FOUND")
        existing = self.repository.list_runs(user_id, workspace_id, experiment_id)
        if any(run["status"] in {"preparing", "running", "paused"} for run in existing):
            raise RuntimeError("EXPERIMENT_ALREADY_RUNNING")
        if existing:
            # Idempotent start: a completed/failed matrix is immutable. A rerun
            # is represented by a new experiment so provenance stays unambiguous.
            return existing
        runs = []
        matrix = [(seed, method_id) for seed in experiment["scenario"]["seedSet"] for method_id in experiment["methods"]]
        random.Random(experiment_id).shuffle(matrix)
        for seed, method_id in matrix:
            method_config = dict((experiment.get("controllerConfig") or {}).get(method_id) or {})
            if method_id in {"C2", "C3", "C4"}:
                # Fairness-sensitive model/sampling fields come from one
                # shared experiment block for all LLM control baselines.
                method_config.update(dict(experiment.get("llmConfig") or {}))
            method_config.setdefault("actionValiditySeconds", experiment["evaluationWindowSeconds"])
            run = self.repository.create_run(
                user_id, workspace_id, experiment_id, method_id, seed, method_config,
                formal=experiment.get("mode", "formal") == "formal",
            )
            run = self.repository.update_run(user_id, workspace_id, run["run_id"], {"provenance": runtime_provenance()})
            runs.append(run)
            self._queue.put({"user_id": user_id, "workspace_id": workspace_id, "experiment": experiment, "run": run})
        self.repository.update_experiment(user_id, workspace_id, experiment_id, {"status": "running"})
        self._ensure_worker()
        return runs

    def pause_run(self, user_id, workspace_id, run_id):
        run, _ = self._require_active_run(user_id, workspace_id, run_id, allowed_statuses={"running"})
        self.gateway.submit({"action": "pause"})
        return self.repository.update_run(user_id, workspace_id, run_id, {"status": "paused"})

    def resume_run(self, user_id, workspace_id, run_id):
        run, _ = self._require_active_run(user_id, workspace_id, run_id, allowed_statuses={"paused"})
        self.gateway.submit({"action": "start"})
        return self.repository.update_run(user_id, workspace_id, run_id, {"status": "running"})

    def stop_run(self, user_id, workspace_id, run_id):
        run, active = self._require_active_run(
            user_id, workspace_id, run_id, allowed_statuses={"preparing", "running", "paused"},
        )
        try:
            self.gateway.submit({"action": "pause"})
        except Exception:
            pass
        updated = self.repository.update_run(user_id, workspace_id, run_id, {"status": "cancelled", "completed_at": datetime.now(timezone.utc)})
        active["terminal"].set()
        self.events.publish("run_state", {"status": "cancelled"}, run_id=run_id)
        return updated

    def _require_active_run(self, user_id, workspace_id, run_id, *, allowed_statuses):
        run = self.repository.get_run(user_id, workspace_id, run_id)
        if not run:
            raise LookupError("RUN_NOT_FOUND")
        with self._lock:
            active = self._active
        if (
            not active
            or active["run"]["run_id"] != str(run_id)
            or active["user_id"] != str(user_id)
            or active["workspace_id"] != str(workspace_id)
            or run.get("status") not in set(allowed_statuses)
        ):
            raise RuntimeError("RUN_NOT_ACTIVE")
        return run, active

    def manual_action(self, user_id, workspace_id, run_id, action, *, reason: str):
        with self._lock:
            active = self._active
        if not active or active["run"]["run_id"] != str(run_id):
            raise RuntimeError("RUN_NOT_ACTIVE")
        if active["user_id"] != str(user_id) or active["workspace_id"] != str(workspace_id):
            raise LookupError("RUN_NOT_FOUND")
        if active["experiment"].get("mode", "formal") != "debug":
            raise RuntimeError("MANUAL_ACTION_FORBIDDEN")
        reason = str(reason or "").strip()
        if not reason:
            raise ContractError("manual action reason is required")
        validated = self.validator.validate(action)
        observation = active.get("last_observation")
        if not observation:
            raise RuntimeError("OBSERVATION_NOT_READY")
        decision = {
            "schemaVersion": "1.0", "decisionId": f"manual_{int(time.time() * 1000)}", "runId": str(run_id),
            "observationId": observation["observationId"], "controllerId": "manual", "controllerVersion": "1.0.0",
            "trigger": {"type": "manual", "operatorUserId": str(user_id)}, "reason": reason, "actions": [validated],
            "expectedEffect": {}, "validFrom": observation["simTimeSeconds"],
            "validUntil": observation["simTimeSeconds"] + float(active["experiment"]["evaluationWindowSeconds"]),
            "algorithmTrace": {}, "modelTrace": None,
        }
        decision = validate_decision(decision)
        self.repository.update_run(user_id, workspace_id, run_id, {"formal": False, "manualIntervention": True})
        self.repository.record("decisions", user_id, workspace_id, run_id, decision)
        pending = {"decision": decision, "before": deepcopy(observation), "applied": 0, "rejected": 0}
        active["pending_evaluations"].append(pending)
        request_id = self.gateway.submit({"action": "apply_action", "decisionId": decision["decisionId"], "controlAction": validated})
        with self._lock:
            self._request_actions[request_id] = {"active": active, "decision": decision, "action": validated, "pending": pending}
        queued = {"schemaVersion": "1.0", "requestId": request_id, "decisionId": decision["decisionId"], "actionId": validated["actionId"], "status": "queued"}
        self.repository.record("acks", user_id, workspace_id, run_id, queued)
        self.events.publish("action_queued", queued, run_id=run_id)
        return queued

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._worker_loop, name="crowdsim-experiment-worker", daemon=True)
                self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._execute_run(item)
            except Exception as exc:
                run = item["run"]
                self.repository.update_run(item["user_id"], item["workspace_id"], run["run_id"], {
                    "status": "failed", "error": f"{type(exc).__name__}: {exc}", "completed_at": datetime.now(timezone.utc),
                })
                self.events.publish("run_failed", {"error": f"{type(exc).__name__}: {exc}"}, run_id=run["run_id"])
                self.logger.exception("CrowdSim run failed")
            finally:
                self.repository.release_gateway_lease(self.owner_id)
                with self._lock:
                    self._request_actions = {
                        request_id: info
                        for request_id, info in self._request_actions.items()
                        if info.get("active") is not self._active
                    }
                    self._active = None
                self._queue.task_done()
            self._finish_experiment_if_done(item["user_id"], item["workspace_id"], item["experiment"]["experiment_id"])

    def _execute_run(self, item: dict[str, Any]) -> None:
        user_id, workspace_id, experiment, run = item["user_id"], item["workspace_id"], item["experiment"], item["run"]
        if not self.repository.acquire_gateway_lease(self.owner_id, ttl_seconds=90):
            raise RuntimeError("SIMULATOR_BUSY")
        if not self.gateway.start(wait_seconds=10):
            raise RuntimeError("CROWDSIM_UNAVAILABLE")
        controller = self.registry.create(run["method_id"], run["config"])
        controller.reset({"runId": run["run_id"], "seed": run["seed"], "userId": user_id, "workspaceId": workspace_id})
        active = {
            "user_id": user_id, "workspace_id": workspace_id, "experiment": experiment, "run": run,
            "controller": controller, "builder": ObservationBuilder(), "last_observation_time": None,
            "last_observation": None, "last_decision": None, "last_effect": None, "pending_evaluations": [],
            "terminal": threading.Event(), "failure": None, "accept_frames": False,
        }
        with self._lock:
            self._active = active
        self.repository.update_run(user_id, workspace_id, run["run_id"], {"status": "preparing", "started_at": datetime.now(timezone.utc)})
        self.events.publish("run_state", {"status": "preparing", "methodId": run["method_id"], "seed": run["seed"]}, run_id=run["run_id"])
        reset_id = self.gateway.submit(self._prepare_reset_payload(experiment["scenario"], run["seed"]))
        reset_result = self.gateway.wait_for_ack(reset_id, timeout=120)
        if not reset_result or reset_result.get("type") == "error":
            raise RuntimeError(f"CrowdSim reset failed: {reset_result}")
        requirement = reset_result.get("requirement") or {}
        if requirement.get("requirement_id"):
            self.repository.update_run(user_id, workspace_id, run["run_id"], {
                "requirement_id": requirement["requirement_id"],
                "population": requirement.get("population_total"),
            })
        self.validator.update_capabilities(self.gateway.capabilities)
        emergency_event = dict(experiment["scenario"].get("emergencyEvent") or {})
        if emergency_event.get("enabled", False):
            event_payload = {
                "action": "set_event",
                "id": f"experiment-emergency-{run['run_id']}",
                "eventType": str(emergency_event.get("eventType") or "fire"),
                "lng": float(emergency_event["lng"]),
                "lat": float(emergency_event["lat"]),
                "radius": float(emergency_event.get("radius", 60.0)),
                "intensity": float(emergency_event.get("intensity", 0.85)),
                "duration": float(emergency_event.get("durationSeconds") or experiment["scenario"]["durationSeconds"]),
                "growthSeconds": float(emergency_event.get("growthSeconds", 30.0)),
                "decaySeconds": float(emergency_event.get("decaySeconds", 0.0)),
                "source": "arde_experiment",
            }
            event_id = self.gateway.submit(event_payload)
            event_result = self.gateway.wait_for_ack(event_id, timeout=30)
            if not event_result or event_result.get("status") != "applied":
                raise RuntimeError(f"CrowdSim emergency event setup failed: {event_result}")
        speed_factor = float(experiment["scenario"].get("speedFactor", 20.0))
        speed_id = self.gateway.submit({"action": "set_speed", "speedFactor": speed_factor})
        speed_result = self.gateway.wait_for_ack(speed_id, timeout=30)
        if not speed_result or speed_result.get("type") == "error":
            raise RuntimeError(f"CrowdSim speed configuration failed: {speed_result}")
        start_id = self.gateway.submit({"action": "start"})
        start_result = self.gateway.wait_for_ack(start_id, timeout=30)
        if not start_result or start_result.get("status") != "applied":
            raise RuntimeError(f"CrowdSim start failed: {start_result}")
        # Reset/start acknowledgements are ordered ahead of the first new run
        # frame.  Arm frame handling only now, so a trailing frame from the
        # preceding run cannot terminate this run before its reset completes.
        active["accept_frames"] = True
        self.repository.update_run(user_id, workspace_id, run["run_id"], {"status": "running"})
        self.events.publish("run_state", {"status": "running"}, run_id=run["run_id"])
        max_wall = float(experiment["scenario"].get("wallTimeoutSeconds", 7200))
        if not active["terminal"].wait(max_wall):
            raise TimeoutError("CrowdSim run exceeded wallTimeoutSeconds")
        if active["failure"]:
            raise RuntimeError(active["failure"])
        current = self.repository.get_run(user_id, workspace_id, run["run_id"])
        if current and current["status"] == "cancelled":
            return
        observations = self.repository.list_records("observations", user_id, workspace_id, run["run_id"], limit=100000)
        summary = summarize_observations(observations)
        self.repository.update_run(user_id, workspace_id, run["run_id"], {
            "status": "completed", "completed_at": datetime.now(timezone.utc), "summary": summary,
            "controller_state": controller.snapshot_state(),
        })
        self.events.publish("run_completed", {"summary": summary}, run_id=run["run_id"])

    def _prepare_reset_payload(self, scenario: dict[str, Any], seed: int) -> dict[str, Any]:
        """Derive an immutable experiment requirement before changing its population."""
        payload = {"action": "reset", "seed": int(seed)}
        count = scenario.get("population")
        if count is None:
            return payload
        payload["count"] = count
        # Ask the runtime rather than using a cached frame: a newly connected
        # gateway may have no init frame, or its latest frame may be stale.
        status_id = self.gateway.submit({"action": "get_status"})
        status = self.gateway.wait_for_ack(status_id, timeout=20)
        if not status or status.get("type") != "status":
            raise RuntimeError(f"CrowdSim requirement lookup failed: {status}")
        requirement = status.get("requirement")
        if not requirement or requirement["population"]["total"] == count:
            return payload
        derived = {"schema_version": requirement["schema_version"]}
        for name in ("project", "spatial_scope", "population", "scenario", "observation"):
            derived[name] = deepcopy(requirement[name])
        derived["population"]["total"] = count
        submission_id = self.gateway.submit({
            "action": "submit_experiment_requirement", "requirement": derived,
        })
        accepted = self.gateway.wait_for_ack(submission_id, timeout=20)
        if (not accepted or accepted.get("type") != "requirement_accepted"
                or not accepted.get("requirement_id")):
            raise RuntimeError(f"CrowdSim experiment requirement failed: {accepted}")
        payload["requirement_id"] = accepted["requirement_id"]
        return payload

    def _on_gateway_event(self, payload: dict[str, Any]) -> None:
        with self._lock:
            active = self._active
        if payload.get("type") == "gateway_state":
            self.events.publish("gateway_state", payload)
            if active and payload.get("state") == "unavailable":
                active["failure"] = payload.get("error") or "gateway unavailable"
                active["terminal"].set()
            return
        if not active:
            return
        if payload.get("type") == "update":
            self._handle_frame(active, payload)
        elif payload.get("type") == "command_result":
            self._handle_ack(active, payload)
        elif payload.get("type") == "error" and payload.get("request_id"):
            self._handle_ack(active, {**payload, "status": "rejected"})

    def _handle_frame(self, active: dict[str, Any], frame: dict[str, Any]) -> None:
        # A fast CrowdSim connection can deliver another update before the
        # asynchronous pause command takes effect.  Once this run has reached
        # a terminal state, ignore those trailing frames so the horizon is
        # recorded exactly once and no action is issued after completion.
        if not active.get("accept_frames", True) or active["terminal"].is_set():
            return
        if not self.repository.acquire_gateway_lease(self.owner_id, ttl_seconds=90):
            active["failure"] = "CrowdSim gateway lease was lost"
            active["terminal"].set()
            return
        run, experiment = active["run"], active["experiment"]
        sim_time = float(frame.get("step_seconds", frame.get("step", 0)) or 0)
        duration = float(experiment["scenario"]["durationSeconds"])
        interval = float(experiment["observationIntervalSeconds"])
        last_time = active["last_observation_time"]
        if last_time is None or sim_time - last_time >= interval or sim_time >= duration:
            observation = active["builder"].build(
                frame, run_id=run["run_id"], last_decision=active["last_decision"], last_effect=active["last_effect"],
            )
            active["last_observation_time"] = sim_time
            active["last_observation"] = observation
            self.repository.record("observations", active["user_id"], active["workspace_id"], run["run_id"], observation)
            self.repository.record("metrics", active["user_id"], active["workspace_id"], run["run_id"], {"simTimeSeconds": sim_time, "global": observation["global"]})
            self.events.publish("observation_created", {"observation": observation}, run_id=run["run_id"])
            self._complete_effect_windows(active, observation)
            context = {"trigger": "critical_risk" if observation["global"]["riskIndex"] >= 0.8 else "observation_cycle", "capabilities": self.gateway.capabilities}
            # The observation at the horizon belongs in the final metrics, but
            # a new decision there cannot affect this run and would leak an
            # action into the following reset.
            if sim_time < duration and active["controller"].should_decide(observation, context):
                self._make_decision(active, observation, context)
        if frame.get("runtime_state") in {"finished", "error", "closed"} or sim_time >= duration:
            if active.get("last_observation") is not None:
                self._complete_effect_windows(active, active["last_observation"], force=True)
            if sim_time >= duration and frame.get("runtime_state") not in {"finished", "error", "closed"}:
                try:
                    self.gateway.submit({"action": "pause"})
                except Exception:
                    pass
            if frame.get("runtime_state") == "error":
                active["failure"] = "CrowdSim entered error state"
            active["terminal"].set()

    def _make_decision(self, active, observation, context):
        decision = validate_decision(active["controller"].decide(observation, context))
        active["last_decision"] = decision
        self.repository.record("decisions", active["user_id"], active["workspace_id"], active["run"]["run_id"], decision)
        if decision.get("modelTrace") is not None:
            self.repository.record("llm", active["user_id"], active["workspace_id"], active["run"]["run_id"], {
                "decisionId": decision["decisionId"], "controllerId": decision["controllerId"], **decision["modelTrace"],
            })
        self.events.publish("decision_created", {"decision": decision}, run_id=active["run"]["run_id"])
        pending = {"decision": decision, "before": deepcopy(observation), "applied": 0, "rejected": 0}
        active["pending_evaluations"].append(pending)
        for raw_action in decision["actions"]:
            try:
                action = self.validator.validate(raw_action)
                request_id = self.gateway.submit({"action": "apply_action", "decisionId": decision["decisionId"], "controlAction": action})
                with self._lock:
                    self._request_actions[request_id] = {"active": active, "decision": decision, "action": action, "pending": pending}
                queued = {"schemaVersion": "1.0", "requestId": request_id, "decisionId": decision["decisionId"], "actionId": action["actionId"], "status": "queued"}
                self.repository.record("acks", active["user_id"], active["workspace_id"], active["run"]["run_id"], queued)
                self.events.publish("action_queued", queued, run_id=active["run"]["run_id"])
            except (ContractError, RuntimeError, ValueError) as exc:
                pending["rejected"] += 1
                action_id = str(raw_action.get("actionId") or f"invalid_{uuid.uuid4().hex}")
                rejected = {"schemaVersion": "1.0", "requestId": f"validation_{action_id}", "decisionId": decision["decisionId"], "actionId": action_id, "status": "rejected", "error": str(exc)}
                active["controller"].on_ack(rejected)
                self.repository.record("acks", active["user_id"], active["workspace_id"], active["run"]["run_id"], rejected)
                self.events.publish("action_rejected", rejected, run_id=active["run"]["run_id"])
        self.repository.update_run(active["user_id"], active["workspace_id"], active["run"]["run_id"], {"controller_state": active["controller"].snapshot_state()})

    def _handle_ack(self, active, payload):
        request_id = str(payload.get("request_id") or payload.get("requestId") or "")
        with self._lock:
            info = self._request_actions.get(request_id)
        if not info:
            return
        status = str(payload.get("status") or "rejected")
        # CrowdSim first acknowledges that a running-simulation action was
        # queued, then emits the terminal applied/rejected result after a tick.
        # Keep the correlation entry for that final result and avoid recording
        # the same queued event twice (it was recorded at submission time).
        if status == "queued":
            return
        with self._lock:
            self._request_actions.pop(request_id, None)
        self.gateway.forget_request(request_id)
        ack = {
            "schemaVersion": "1.0", "requestId": request_id, "decisionId": info["decision"]["decisionId"],
            "actionId": info["action"]["actionId"], "status": status,
            "submittedAt": payload.get("submitted_at"), "appliedAt": payload.get("applied_at"),
            "snapshotId": payload.get("snapshot_id"), "detail": payload.get("detail"),
            "error": payload.get("message"),
        }
        info["pending"]["applied" if status == "applied" else "rejected"] += 1
        active["controller"].on_ack(ack)
        self.repository.record("acks", active["user_id"], active["workspace_id"], active["run"]["run_id"], ack)
        self.events.publish(f"action_{status}", ack, run_id=active["run"]["run_id"])

    def _complete_effect_windows(self, active, observation, *, force=False):
        window = float(active["experiment"]["evaluationWindowSeconds"])
        keep = []
        for pending in active["pending_evaluations"]:
            if pending["applied"] <= 0:
                keep.append(pending)
                continue
            window_complete = observation["simTimeSeconds"] >= pending["before"]["simTimeSeconds"] + window
            if not force and not window_complete:
                keep.append(pending)
                continue
            evaluation = self.evaluator.evaluate(pending["decision"], pending["before"], observation)
            evaluation["windowCensored"] = not window_complete
            learning = active["controller"].on_effect(evaluation)
            if learning is not None:
                evaluation["learning"] = learning
            active["last_effect"] = evaluation
            self.repository.record("evaluations", active["user_id"], active["workspace_id"], active["run"]["run_id"], evaluation)
            self.events.publish("evaluation_completed", {"evaluation": evaluation}, run_id=active["run"]["run_id"])
        active["pending_evaluations"] = keep

    def _finish_experiment_if_done(self, user_id, workspace_id, experiment_id):
        runs = self.repository.list_runs(user_id, workspace_id, experiment_id)
        if runs and all(run["status"] in {"completed", "failed", "cancelled"} for run in runs):
            status = "completed" if all(run["status"] == "completed" for run in runs) else "completed_with_failures"
            self.repository.update_experiment(user_id, workspace_id, experiment_id, {"status": status})
