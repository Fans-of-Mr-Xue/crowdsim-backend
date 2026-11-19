"""Structured per-person decision evidence chain (read-only projection).

The trace assembles, for one decision boundary, the confirmed stages already
produced by the production runtime: observation -> state -> PAD/desire ->
goal intent -> bounded game option table -> legal candidate scores -> final
plan and constraints -> SUMO receipt -> learning audit events (sample /
update before-after / next choice).

Design rules:

- Read-only: this module never evaluates affect, invokes the decision owner,
  scores or updates a learner, touches SUMO, reseeds RNG or mutates counters.
  Replayed game tables use the deterministic pure-function mirror only.
- Additive: existing contracts, plans, receipts and archive formats keep
  their original fields; trace fields live under their own versioned block.
- No LLM追认: every value is taken from a backend production structure; an
  absent stage is reported as ``null``/``missing`` rather than reconstructed
  from narrative.

Mechanism naming is fixed: ``utility_weighted_probabilistic`` denotes the
quantal/softmax utility-weighted response in the bounded game model. It is
not a complete strategic game equilibrium.

Participation states (additive enum, one per legal arm / learning effect):

- ``participated_unchanged``: the arm entered the legal set but the final plan
  kept the baseline arm / the learner did not change the choice.
- ``constraint_blocked``: the arm was kept out of the legal set by a confirmed
  protection (locked itinerary, activity dwell, policy wait, flow hold,
  external control, hotspot activity).
- ``downstream_refined``: the baseline suggestion was refined to this legal
  candidate / arm by the decision owner or the tracked learning selection.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
import json
import math
from pathlib import Path
from typing import Any, Iterable

VERSION = "decision-trace-v1"

LOCAL_ARMS = ("continue", "slow_down", "wait")
ROUTE_ARMS = ("reroute", "change_goal")
PARTICIPATION_STATES = ("participated_unchanged", "constraint_blocked", "downstream_refined")

# Audit event kinds emitted by the learning controller that the trace relates.
_LEARNING_KINDS = {
    "selection", "learning_update", "interval", "sample_unavailable",
    "execution_not_applied", "untracked_plan",
}


def _finite(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    return value


def _motion_fields(motion: Any) -> dict[str, Any]:
    if motion is None:
        return {}
    raw = asdict(motion) if is_dataclass(motion) else dict(getattr(motion, "__dict__", motion))
    keep = ("person_id", "time_seconds", "x", "y", "lon", "lat", "speed", "edge_id",
            "lane_id", "lane_position", "angle", "stage_index", "stage_type",
            "remaining_stage_count", "departed")
    return {key: _finite(raw.get(key)) for key in keep if key in raw}


def _observation_fields(obs: Any) -> dict[str, Any]:
    if obs is None:
        return {"status": "missing_observation"}
    return {
        "status": "observed",
        "snapshot_id": obs.snapshot_id,
        "time_seconds": obs.time_seconds,
        "neighbour_ids": list(obs.neighbour_ids),
        "local_people_count": obs.local_people_count,
        "local_area_m2": _finite(obs.local_area_m2),
        "objective_density_per_m2": _finite(obs.objective_density_per_m2),
        "perceived_crowding": _finite(obs.perceived_crowding),
        "perceived_risk": _finite(obs.perceived_risk),
        "density_level": obs.density_level,
        "flood_impact": _finite(obs.flood_impact),
        "event_impact": _finite(obs.event_impact),
        "known_event_ids": list(obs.known_event_ids),
        "available_goal_ids": list(obs.available_goal_ids),
        "motion": _motion_fields(obs.own_motion),
    }


def _affect_input_factors(state: Any, obs: Any, profile: Any) -> dict[str, Any]:
    """Snapshot the real state/observation factors entering PAD smoothing.

    These are the raw boundary readings the emotion model consumes; the PAD
    result itself is projected separately. The production pipeline archives the
    carried-in previous-moment PAD (``before_pad``) and the produced PAD
    (``after_pad``) in ``_affect_contexts``; the first boundary has no prior PAD.
    """
    def num(value: Any) -> float | None:
        return _finite(value) if isinstance(value, (int, float)) else None

    state_risk = getattr(state, "perceived_risk", None)
    obs_risk = getattr(obs, "perceived_risk", None)
    risk = max(state_risk, obs_risk) if isinstance(state_risk, (int, float)) and isinstance(obs_risk, (int, float)) else (state_risk if isinstance(state_risk, (int, float)) else obs_risk)
    speed = getattr(getattr(obs, "own_motion", None), "speed", None)
    free = getattr(profile, "free_walking_speed", None)
    mobility = getattr(profile, "mobility", None)
    speed_ratio = None
    if isinstance(speed, (int, float)) and isinstance(free, (int, float)) and isinstance(mobility, (int, float)) and free * mobility > 0:
        speed_ratio = speed / (free * mobility)
    blocked = getattr(state, "blocked_duration", None)
    return {
        "risk": num(risk),
        "crowding": num(getattr(obs, "perceived_crowding", None)),
        "stress": num(getattr(state, "stress", None)),
        "fatigue": num(getattr(state, "fatigue", None)),
        "blockage": (blocked / 30.0 if isinstance(blocked, (int, float)) else None),
        "speed_ratio": num(speed_ratio),
    }


def _state_fields(state: Any) -> dict[str, Any]:
    if state is None:
        return {}
    keep = ("person_id", "stress", "fatigue", "perceived_risk", "perceived_crowding",
            "blocked_duration", "activity_state", "current_goal", "pending_goal",
            "poi_plan_active", "activity_plan", "next_decision_time",
            "hotspot_entry_edge", "hotspot_activity_phase", "group_id",
            "companion_ids", "rendezvous_id")
    raw = asdict(state) if is_dataclass(state) else dict(getattr(state, "__dict__", state))
    return {key: _finite(raw.get(key)) if isinstance(raw.get(key), (int, float)) else raw.get(key)
            for key in keep if key in raw}


def _constraints_fields(constraints: Any) -> dict[str, Any] | None:
    if constraints is None:
        return None
    if not isinstance(constraints, dict):
        return None
    return deepcopy(constraints)


def _constraint_blocked(constraints: dict[str, Any] | None, state: Any, locked: bool) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if locked:
        reasons.append("locked_itinerary")
    if constraints:
        if constraints.get("control_limits"):
            reasons.append("external_control")
        if constraints.get("activity_hold"):
            reasons.append("activity_dwell")
        if constraints.get("policy_wait"):
            reasons.append("policy_wait")
        flow_hold = constraints.get("flow_hold")
        if flow_hold:
            reasons.append("flow_hold")
    if state is not None and getattr(state, "poi_plan_active", False):
        reasons.append("hotspot_activity")
    if state is not None and getattr(state, "activity_state", None) in {"hotspot_dwelling", "hotspot_departing"}:
        reasons.append("hotspot_activity")
    return bool(reasons), reasons


def _candidate_card(candidate: Any) -> dict[str, Any] | None:
    """Read-only identity/cost projection of one supplied legal boundary candidate."""
    if candidate is None:
        return None
    raw = asdict(candidate) if is_dataclass(candidate) else dict(getattr(candidate, "__dict__", candidate))
    action = "change_goal" if raw.get("target_kind") == "activity" else "reroute"
    return {
        "candidate_id": raw.get("candidate_id"),
        "action": action,
        "target_id": raw.get("target_id"),
        "target_kind": raw.get("target_kind"),
        "target_edge": raw.get("target_edge"),
        "entry_edge": raw.get("entry_edge"),
        "edges": list(raw.get("edges") or ()),
        "arrival_position": _finite(raw.get("arrival_position")),
        "estimated_cost_seconds": _finite(raw.get("estimated_cost_seconds")),
        "base_cost_seconds": _finite(raw.get("base_cost_seconds")),
        "congestion_delay_seconds": _finite(raw.get("congestion_delay_seconds")),
        "entry_density_person_per_m2": _finite(raw.get("entry_density_person_per_m2")),
        "activity_duration": _finite(raw.get("activity_duration")),
        "next_target_id": raw.get("next_target_id"),
        "next_route_edges": list(raw.get("next_route_edges") or ()),
        "next_arrival_position": _finite(raw.get("next_arrival_position")),
        "minimum_savings_seconds": _finite(raw.get("minimum_savings_seconds")),
        "switch_cooldown_seconds": _finite(raw.get("switch_cooldown_seconds")),
    }


def _sample_summary(sample: Any) -> dict[str, Any] | None:
    """Read-only immutable projection of a real accepted/queued sample.

    Only encoded state/feature vectors and attribution fields are projected;
    no API key, raw prompt or model credential is carried. Absent fields stay
    null rather than filled from a default.
    """
    if sample is None:
        return None
    raw = asdict(sample) if is_dataclass(sample) else dict(sample)
    if not isinstance(raw, dict):
        return None
    return {
        "sample_id": raw.get("sample_id"),
        "action": deepcopy(raw.get("action")),
        "target_id": raw.get("target_id"),
        "candidate_id": raw.get("candidate_id"),
        "state": deepcopy(raw.get("state")),
        "features": deepcopy(raw.get("features")),
        "next_state": deepcopy(raw.get("next_state")),
        "next_actions": deepcopy(raw.get("next_actions") or []),
        "reward": _finite(raw.get("reward")),
        "duration_seconds": _finite(raw.get("duration_seconds")),
        "evidence_id": raw.get("evidence_id"),
        "feedback_rule_id": raw.get("feedback_rule_id"),
        "feedback_version": raw.get("feedback_version"),
        "predicted_cost_seconds": _finite(raw.get("predicted_cost_seconds")),
        "observed_cost_seconds": _finite(raw.get("observed_cost_seconds")),
        "cost_complete": raw.get("cost_complete"),
        "terminated": raw.get("terminated"),
        "truncated": raw.get("truncated"),
    }


_UPDATE_KEYS = ("method_id", "binding_fingerprint", "sample_id", "evidence_id",
    "update_version", "update_token", "sample_digest", "feedback", "replay",
    "before", "after", "detail", "sample")


def _update_public(update: Any) -> dict[str, Any]:
    """Whitelist projection of one per-sample learner update record."""
    if not isinstance(update, dict):
        return {}
    return {key: deepcopy(update.get(key)) for key in _UPDATE_KEYS}


def _learning_event(pid: str, row: str | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(row, dict):
        event = row
    else:
        try:
            event = json.loads(row)
        except (ValueError, TypeError):
            return None
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    event_pid = event.get("person_id")
    if event_pid is None:
        event_pid = evidence.get("person_id")
    if not isinstance(event, dict) or event_pid != pid or event.get("kind") not in _LEARNING_KINDS:
        return None
    kind = event["kind"]
    eval_time = event.get("evaluation", {}).get("time_seconds") if isinstance(event.get("evaluation"), dict) else None
    out: dict[str, Any] = {"kind": kind, "snapshot_id": event.get("snapshot_id"),
                            "time_seconds": _finite(event.get("time_seconds") if event.get("time_seconds") is not None else eval_time),
                            "record_id": event.get("record_id")}
    if kind == "selection":
        baseline = event.get("baseline") or {}
        selected = event.get("selected") or {}
        out.update({
            "policy": event.get("policy"),
            "baseline_action": baseline.get("proposed_action"),
            "baseline_target_id": baseline.get("target_id"),
            "baseline_candidate_id": baseline.get("candidate_id"),
            "baseline_snapshot_id": baseline.get("snapshot_id"),
            "selected_action": selected.get("proposed_action"),
            "selected_target_id": selected.get("target_id"),
            "selected_candidate_id": selected.get("candidate_id"),
            "selected_snapshot_id": selected.get("snapshot_id"),
            "scores": deepcopy(event.get("scores") or []),
        })
    elif kind == "learning_update":
        sample = event.get("sample") or {}
        evaluation = event.get("evaluation") or {}
        before_enc = event.get("before_encoding") or {}
        out.update({
            "interval_id": event.get("interval_id"),
            "acceptance": event.get("acceptance"),
            # Origin decision boundary (pre-execution) vs the feedback-after
            # boundary where the terminal sample was produced; a completed-route
            # sample can land on a later boundary than the origin decision.
            "origin_snapshot_id": before_enc.get("snapshot_id"),
            "origin_time_seconds": _finite(before_enc.get("time_seconds")),
            "after_snapshot_id": event.get("snapshot_id"),
            "after_time_seconds": _finite(event.get("time_seconds")),
            "evidence_id": evaluation.get("evidence_id") or sample.get("evidence_id"),
            "accepted_sample": {
                "sample_id": sample.get("sample_id"),
                "action": sample.get("action"),
                "target_id": sample.get("target_id"),
                "candidate_id": sample.get("candidate_id"),
                "state": deepcopy(sample.get("state")),
                "features": deepcopy(sample.get("features")),
                "next_state": deepcopy(sample.get("next_state")),
                "reward": _finite(sample.get("reward")),
                "duration_seconds": _finite(sample.get("duration_seconds")),
                "evidence_id": sample.get("evidence_id"),
                "feedback_rule_id": sample.get("feedback_rule_id"),
                "feedback_version": sample.get("feedback_version"),
                "predicted_cost_seconds": _finite(sample.get("predicted_cost_seconds")),
                "observed_cost_seconds": _finite(sample.get("observed_cost_seconds")),
                "cost_complete": sample.get("cost_complete"),
                "terminated": sample.get("terminated"),
                "truncated": sample.get("truncated"),
                "next_actions": deepcopy(sample.get("next_actions") or []),
            },
            "evaluation": {"reward": _finite(evaluation.get("reward")),
                           "end_reason": evaluation.get("end_reason")},
            "before_encoding": deepcopy(event.get("before_encoding")),
            "next_encodings": deepcopy(event.get("next_encodings") or []),
            "updates": [_update_public(u) for u in (event.get("updates") or [])],
        })
    elif kind == "interval":
        plan = evidence.get("plan") or {}
        receipt = evidence.get("receipt") or {}
        out.update({"interval_id": evidence.get("interval_id"), "trainable": evidence.get("trainable"),
                    "end_reason": evidence.get("end_reason"), "reward": _finite(evidence.get("reward")),
                    "duration_seconds": _finite(evidence.get("duration_seconds")),
                    "risk_exposure_seconds": _finite(evidence.get("risk_exposure_seconds")),
                    "started_at": _finite(evidence.get("started_at")),
                    "ended_at": _finite(evidence.get("ended_at")),
                    "origin_snapshot_id": plan.get("snapshot_id"),
                    "origin_time_seconds": _finite(plan.get("decided_at")),
                    "applied_action": receipt.get("applied_action"),
                    "receipt_status": receipt.get("status"),
                    "applied_at": _finite(receipt.get("applied_at"))})
    elif kind == "sample_unavailable":
        out.update({"interval_id": event.get("interval_id"), "reason": event.get("reason")})
    elif kind == "execution_not_applied":
        receipt = event.get("receipt") or {}
        out.update({"receipt_status": receipt.get("status"), "receipt_reason": receipt.get("reason")})
    elif kind == "untracked_plan":
        out.update({"reason": event.get("reason")})
    return out


def _learning_block(runtime: Any, pid: str) -> dict[str, Any]:
    controller = getattr(runtime, "learning_controller", None)
    if controller is None:
        return {"attached": False, "events": []}
    role = runtime.population.profile_for(pid).crowd_role
    role_setting = controller.roles.get(role)
    events = [event for event in (_learning_event(pid, row) for row in controller.batch()) if event]
    last_update = next((event for event in reversed(events) if event["kind"] == "learning_update"), None)
    # Selection count is attributed per person from this person's own selection
    # events; the controller-global ``selection_count`` must not be projected
    # onto a person who has no personal learner binding.
    selections = sum(1 for event in events if event["kind"] == "selection")

    def _unattached(method_id: Any, integration_mode: Any, interval_mode: Any) -> dict[str, Any]:
        return {
            "attached": False,
            "method_id": method_id,
            "integration_mode": integration_mode,
            "interval_mode": interval_mode,
            "accepted_samples": 0,
            "update_version": 0,
            "pending_or_replay_samples": 0,
            "selections": selections,
            "binding": None,
            "replay": {"enabled": False, "buffer": {"pending_or_replay_samples": 0,
                                                    "sample_ids": [], "samples": []}},
            "events": events,
            "last_update": last_update,
        }

    # The role is not configured for learning on this controller. Projecting the
    # controller-global accepted/update/selection totals onto such a person would
    # attribute other roles' learning to them; report not attached with zeroed
    # per-person counters instead.
    if role_setting is None:
        return _unattached(None, None, None)

    setting = role_setting.get("setting", {})
    method_id = (setting.get("learning_config") or {}).get("method_id")
    learner = controller.learners.get(pid)
    bound = False
    if learner is not None:
        identity = learner.binding.identity
        if (identity.run_id == controller.run_id and identity.role_id == role
                and identity.subject_id == pid
                and identity.model_fingerprint == controller.model_fingerprint):
            bound = True

    # Configured role but the per-person learner is not instantiated, or its
    # binding identity does not match this run/role/subject/model fingerprint.
    if not bound:
        return _unattached(method_id, setting.get("integration_mode"), setting.get("interval_mode"))

    try:
        learner_view = deepcopy(learner.diagnostics())
    except Exception:
        learner_view = {}
    replay_raw = getattr(learner, "replay", None)
    replay_view = deepcopy(replay_raw) if isinstance(replay_raw, dict) else None
    stored_samples = list(getattr(learner, "samples", ()))
    sample_ids = [getattr(s, "sample_id", None) for s in stored_samples]
    sample_summaries = [summary for summary in (_sample_summary(s) for s in stored_samples) if summary]
    pending = learner_view.get("pending_or_replay_samples", len(sample_ids))
    replay_with_samples = dict(replay_view) if replay_view is not None else {"enabled": False}
    replay_with_samples["buffer"] = {"pending_or_replay_samples": pending,
                                     "sample_ids": sample_ids, "samples": sample_summaries}
    return {
        "attached": True,
        "method_id": method_id,
        "integration_mode": setting.get("integration_mode"),
        "interval_mode": setting.get("interval_mode"),
        "accepted_samples": learner_view.get("accepted_samples", 0),
        "update_version": learner_view.get("update_version", 0),
        "pending_or_replay_samples": pending,
        "selections": selections,
        "binding": {k: learner_view.get(k) for k in ("binding_fingerprint", "frozen")},
        "replay": replay_with_samples,
        "events": events,
        "last_update": last_update,
    }


def _game_block(engine: Any, profile: Any, state: Any, obs: Any,
                emotion: Any, desire: Any, candidates: tuple[Any, ...],
                choice_ctx: dict[str, Any] | None = None,
                affect_available: bool = False) -> dict[str, Any]:
    role = profile.crowd_role
    assembly = getattr(engine, "assemblies", {}).get(role)
    game = getattr(assembly, "game", None) if assembly is not None else None
    game = game or getattr(engine, "game", None)
    block: dict[str, Any] = {"mode": "unavailable", "rationality": None, "options": [],
                             "selected": None, "selected_reason": None, "recommended": None,
                             "selection_source": None, "action_bias": None,
                             "affect_evaluated": affect_available}
    if game is None:
        block["selected_reason"] = "game_model_not_configured"
        return block

    boundary_match = choice_ctx is not None and choice_ctx.get("key") == (obs.snapshot_id, obs.time_seconds)
    options = None
    if boundary_match and choice_ctx.get("options") is not None:
        # The frozen production table and its actual draw are projected
        # independently of the affect boundary: a role with emotion disabled
        # still runs the game gate, so its real options/choice are not hidden
        # behind an evaluated-affect gate.
        options = choice_ctx["options"]
        block["mode"] = choice_ctx.get("selection", "utility_weighted_probabilistic")
        block["rationality"] = _finite(choice_ctx.get("rationality"))
        block["action_bias"] = deepcopy(choice_ctx.get("action_bias") or {})
        selection_source = "production_frozen"
    else:
        # Mirror recompute needs an evaluated PAD/desire boundary; without one
        # refuse rather than reconstruct a neutral fallback table.
        if emotion is None or desire is None:
            block["selected_reason"] = "evaluated_affect_boundary_missing"
            return block
        evaluate = getattr(game, "evaluate_options", None)
        if evaluate is None:
            block["selected_reason"] = "game_model_has_no_readonly_table"
            return block
        try:
            table = evaluate(profile, state, obs, emotion, desire, candidates)
        except Exception:
            block["selected_reason"] = "game_table_recompute_failed"
            return block
        options = table.get("options", [])
        block["mode"] = table.get("selection", "utility_weighted_probabilistic")
        block["rationality"] = _finite(table.get("rationality"))
        selection_source = "mirror"

    block["selection_source"] = selection_source
    block["options"] = [
        {key: _finite(value) if key in ("utility", "probability") else value
         for key, value in option.items()}
        for option in options
    ]
    if boundary_match and choice_ctx.get("choice"):
        ch = choice_ctx["choice"]
        block["selected"] = {key: deepcopy(ch.get(key)) if key == "probabilities" else ch.get(key)
                             for key in ("action", "target_id", "candidate_id", "utility", "probabilities", "reason")}
    else:
        block["selected_reason"] = "no_production_choice_recorded"

    if block["options"]:
        top = max(block["options"],
                  key=lambda option: ((option.get("probability") or 0.0), (option.get("utility") or -math.inf)))
        block["recommended"] = {key: top.get(key) for key in
                               ("action", "target_id", "candidate_id", "utility", "probability")}
    else:
        block["recommended"] = None
    return block


def _llm_block(engine: Any, profile: Any) -> dict[str, Any] | None:
    role = profile.crowd_role
    assembly = getattr(engine, "assemblies", {}).get(role)
    decision = getattr(assembly, "decision", None) if assembly is not None else None
    if decision is None:
        return None
    diagnostics = {}
    try:
        diagnostics = decision.diagnostics()
    except Exception:
        diagnostics = {}
    return {
        "decision_source": "llm_driven",
        "callable": getattr(decision, "callable", None),
        "online_mode": getattr(decision, "online_mode", None),
        "successes": diagnostics.get("successes"),
        "calls": diagnostics.get("calls"),
        "fallbacks": diagnostics.get("fallbacks"),
        "advisory_score_count": diagnostics.get("advisory_scores"),
        "last_error": diagnostics.get("last_error") or None,
    }


def _participation(plan: Any, candidates: tuple[Any, ...], constraints: dict[str, Any] | None,
                   state: Any, locked: bool, selection: dict[str, Any] | None) -> list[dict[str, Any]]:
    blocked, reasons = _constraint_blocked(constraints, state, locked)
    entries: list[dict[str, Any]] = []
    plan_candidate = getattr(plan, "candidate_id", None)
    plan_action = getattr(plan, "proposed_action", None)
    for candidate in candidates:
        cid = candidate.candidate_id
        if cid is not None and cid == plan_candidate:
            state_label = "downstream_refined"
            why = "final plan selected this supplied legal candidate"
        elif blocked:
            state_label = "constraint_blocked"
            why = "; ".join(reasons)
        else:
            state_label = "participated_unchanged"
            why = "legal candidate present but baseline plan kept another arm"
        entries.append({
            "candidate_id": cid,
            "action": "change_goal" if candidate.target_kind == "activity" else "reroute",
            "target_id": candidate.target_id,
            "target_kind": candidate.target_kind,
            "state": state_label,
            "reason": why,
        })
    # Local arms are reported alongside route candidates when no route candidate
    # was selected; protection reasons still apply to every arm.
    if plan_action in LOCAL_ARMS:
        for arm in LOCAL_ARMS:
            if any(entry["action"] == arm for entry in entries):
                continue
            if arm == plan_action:
                label, why = "downstream_refined", "final plan selected this local action"
            elif blocked:
                label, why = "constraint_blocked", "; ".join(reasons)
            else:
                label, why = "participated_unchanged", "local alternative present but baseline kept another arm"
            entries.append({"candidate_id": None, "action": arm, "target_id": None,
                             "target_kind": "local", "state": label, "reason": why})
    if selection is not None:
        refined = selection.get("selected_action") != selection.get("baseline_action") or (
            selection.get("selected_candidate_id") != selection.get("baseline_candidate_id"))
        entries.append({
            "candidate_id": selection.get("selected_candidate_id"),
            "action": selection.get("selected_action"),
            "target_id": selection.get("selected_target_id"),
            "target_kind": "learning_selection",
            "state": "downstream_refined" if refined else "participated_unchanged",
            "reason": "tracked learning selection changed the baseline" if refined
                      else "tracked learning selection kept the baseline arm",
            "policy": selection.get("policy"),
        })
    return entries


def build_decision_trace(runtime: Any, profile: Any, state: Any, *, candidates: Iterable[Any] | None = None,
                         before: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the read-only evidence trace for one person at the current boundary.

    Missing stages are explicit ``null``/``missing``; the function never
    invokes decision, learning or SUMO machinery.

    ``before`` is the validated pre-decision light snapshot frozen by the
    runtime before any plan is resolved/applied. When the production affect
    pipeline has no frozen boundary context (e.g. an engine-less baseline), the
    boundary segment still projects this genuine decision-before state/obs;
    a post-execution state is never substituted for it.
    """
    pid = profile.person_id
    engine = getattr(runtime, "decision_engine", None)
    obs = runtime.observations.get(pid) if getattr(runtime, "observations", None) is not None else None
    motion = runtime.current.persons.get(pid) if getattr(runtime, "current", None) is not None else None

    person_match = (state is not None and state.person_id == pid
                    and (obs is None or obs.person_id == pid))
    boundary_ok = (person_match and obs is not None
                   and obs.snapshot_id == runtime.snapshot_id
                   and isinstance(obs.time_seconds, (int, float))
                   and math.isfinite(obs.time_seconds)
                   and obs.time_seconds == runtime.time_seconds)

    candidates_tuple = tuple(candidates if candidates is not None else
                              runtime.decision_candidates.get(pid, ()) if getattr(runtime, "decision_candidates", None) is not None else ())

    config = getattr(engine, "role_configs", {}).get(profile.crowd_role) if engine is not None else None
    emotion_enabled = bool(config.capabilities.get("emotion")) if config is not None else False
    desire_enabled = bool(config.desire_enabled) if config is not None else False

    # Frozen affect boundary reused by the production pipeline, validated to
    # the current runtime boundary before any field is projected. A missing
    # boundary leaves emotion/desire null; no neutral PAD/desire fallback is
    # reconstructed. Capability flags are projected separately so a disabled
    # module is not dressed up as evaluated affect.
    emotion = desire = None
    affect_status = "disabled" if config is not None else "no_role_config"
    affect_at = None
    boundary_context = None
    choice_ctx = None
    affect_ctx = None
    if engine is not None:
        cached = getattr(engine, "_boundary_contexts", {}).get(pid)
        if cached and boundary_ok:
            key, context = cached
            if key == (obs.snapshot_id, obs.time_seconds):
                emotion, desire, _, _, _ = context
                boundary_context = context
                affect_status = "evaluated"
                affect_at = {"snapshot_id": key[0], "time_seconds": key[1]}
        if emotion_enabled:
            measured = getattr(engine, "emotional_states", {}).get(pid)
            if measured is not None and boundary_ok:
                emotion = emotion or measured
                if affect_status in ("disabled", "no_role_config"):
                    affect_status = "evaluated"
                    affect_at = {"snapshot_id": obs.snapshot_id, "time_seconds": obs.time_seconds}
        if affect_status != "evaluated":
            affect_status = "not_enabled" if config is not None else "no_role_config"
        affect_ctx = getattr(engine, "_affect_contexts", {}).get(pid)
        if affect_ctx is not None and (not boundary_ok or affect_ctx.get("key") != (obs.snapshot_id, obs.time_seconds)):
            affect_ctx = None
        choice_ctx = getattr(engine, "_choice_contexts", {}).get(pid)

    goal_trace = engine.goal_trace(pid) if engine is not None else None
    if goal_trace is not None and not boundary_ok:
        goal_trace = None

    # Validate the runtime-frozen pre-decision copy against the current
    # boundary identity before it may back the boundary segment.
    before_state = None
    before_obs = None
    before_match = False
    if before is not None and boundary_ok:
        b_state = before.get("state")
        b_obs = before.get("observation")
        b_snap = before.get("snapshot_id")
        b_time = before.get("time_seconds")
        if (b_state is not None and b_snap == runtime.snapshot_id
                and isinstance(b_time, (int, float)) and math.isfinite(b_time)
                and b_time == runtime.time_seconds
                and (b_obs is None or b_obs.person_id == pid)):
            before_state, before_obs = b_state, b_obs
            before_match = True

    game_state = state
    game_obs = obs
    boundary_state_obj = None
    boundary_state_status = "missing_before_state"
    if boundary_context is not None:
        _, desire_c, frozen_profile, frozen_state, frozen_obs = boundary_context
        if frozen_profile.person_id == pid:
            game_state, game_obs = frozen_state, frozen_obs
            boundary_state_obj = frozen_state
            boundary_state_status = "frozen_boundary"
    elif before_match:
        # Engine-less/affect-disabled baseline still carries the real
        # decision-before state/obs frozen by the runtime; no after-state fill.
        game_state, game_obs = before_state, before_obs
        boundary_state_obj = before_state
        boundary_state_status = "runtime_before_snapshot"
    if not boundary_ok:
        boundary_state_obj = None
        boundary_state_status = "boundary_not_confirmed"
    if engine is not None:
        game_block = _game_block(engine, profile, game_state, game_obs, emotion, desire,
                                 candidates_tuple, choice_ctx=choice_ctx,
                                 affect_available=affect_status == "evaluated")
    else:
        game_block = {"mode": "unavailable", "rationality": None, "options": [], "selected": None,
                      "selected_reason": "no_decision_engine", "recommended": None,
                      "selection_source": None, "action_bias": None, "affect_evaluated": False}

    plan = state.current_plan if state is not None else None
    evidence = getattr(getattr(runtime, "plan_executor", None), "execution_evidence", {}).get(pid)
    receipt = None
    if evidence is not None and boundary_ok:
        receipt = deepcopy(evidence.get("receipt"))
        # Contract aliases: requested_action mirrors proposed_action,
        # time_seconds mirrors applied_at; original fields are retained.
        receipt.setdefault("requested_action", receipt.get("proposed_action"))
        receipt.setdefault("time_seconds", receipt.get("applied_at"))
    elif evidence is not None:
        receipt = None

    constraints = None
    post_constraints = getattr(runtime, "_post_action_constraints", None)
    if callable(post_constraints):
        try:
            constraints = _constraints_fields(post_constraints(pid))
        except Exception:
            constraints = None

    locked = pid in getattr(runtime.population, "locked_itinerary_ids", set())
    learning = _learning_block(runtime, pid)
    selection = next((event for event in reversed(learning["events"]) if event["kind"] == "selection"), None)

    scores = getattr(runtime, "_learning_scores", {}).get(pid)
    if not scores and engine is not None:
        scores = getattr(engine, "_candidate_scores", {}).get(pid)
    candidate_scores = deepcopy(scores or {})
    llm = _llm_block(engine, profile) if engine is not None else None

    participation = _participation(plan, candidates_tuple, constraints, state, locked, selection)
    candidate_cards = [card for card in (_candidate_card(c) for c in candidates_tuple) if card is not None]

    trace = {
        "version": VERSION,
        "run_id": runtime.run_id,
        "snapshot_id": runtime.snapshot_id,
        "time_seconds": _finite(runtime.time_seconds),
        "person_id": pid,
        "role_id": profile.crowd_role,
        "contract": getattr(engine, "execution_contract", None),
        "boundary_ok": boundary_ok,
        "observation": _observation_fields(obs),
        "motion": _motion_fields(motion),
        "state": _state_fields(state),
        # Explicit two-phase split: ``boundary`` is the pre-decision snapshot,
        # ``post_execution`` is the state/plan left after the action was applied.
        # The top-level ``state``/``plan`` fields below are the post-execution
        # projection kept for backward compatibility.
        "boundary": {
            "phase": "before_decision",
            "snapshot_id": runtime.snapshot_id,
            "time_seconds": _finite(runtime.time_seconds),
            "observation": _observation_fields(game_obs),
            "motion": _motion_fields(motion),
            "state": _state_fields(boundary_state_obj) if boundary_state_obj is not None else None,
            "state_status": boundary_state_status,
        },
        "post_execution": {
            "phase": "after_decision",
            "snapshot_id": runtime.snapshot_id,
            "time_seconds": _finite(runtime.time_seconds),
            "state": _state_fields(state),
            "plan": asdict(plan) if plan is not None else None,
            "receipt": receipt,
            "constraints": constraints,
        },
        "affect": {
            "status": affect_status,
            "evaluated_at": affect_at,
            "emotion_enabled": emotion_enabled,
            "desire_enabled": desire_enabled,
            "continuity": (affect_ctx or {}).get("continuity") or ("first_boundary" if emotion_enabled else "disabled"),
            "before_pad": (affect_ctx or {}).get("before_pad") if affect_ctx is not None else None,
            "previous_pad": (affect_ctx or {}).get("before_pad") if affect_ctx is not None else None,
            "after_pad": (affect_ctx or {}).get("after_pad") if affect_ctx is not None else (emotion.as_dict() if emotion is not None else None),
            "pad": emotion.as_dict() if emotion is not None else None,
            "desire": desire.as_dict() if desire is not None else None,
            "input_factors": ((affect_ctx or {}).get("input_factors") if affect_ctx is not None
                              else _affect_input_factors(state, obs, profile)) if emotion_enabled else None,
        },
        "goal_intent": deepcopy(goal_trace),
        "game": game_block,
        "candidates": candidate_cards,
        "candidate_scores": candidate_scores,
        "llm": llm,
        "plan": asdict(plan) if plan is not None else None,
        "constraints": constraints,
        "receipt": receipt,
        "learning": learning,
        "participation": participation,
        "guard": {"locked_itinerary": locked, "protected_reasons": _constraint_blocked(constraints, state, locked)[1]},
    }
    return trace


class DecisionTraceStore:
    """Bounded in-memory index plus append-only JSONL persistence for traces.

    Index keys are ``(run_id, person_id, snapshot_id, time_seconds)``. Records
    are appended in production order; history reads query the same source that
    the live view projects, so a reconnect after a poll reads identical rows.
    """

    def __init__(self, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("trace capacity must be positive")
        self.capacity = capacity
        self._records: list[dict[str, Any]] = []
        self._by_key: dict[tuple[str, str, str, float], int] = {}

    def record(self, trace: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(trace, dict) or trace.get("version") != VERSION:
            raise ValueError("invalid decision trace")
        key = (str(trace["run_id"]), str(trace["person_id"]),
               str(trace.get("snapshot_id") or ""), _finite(trace.get("time_seconds")) or 0.0)
        if key in self._by_key:
            self._records[self._by_key[key]] = deepcopy(trace)
        else:
            if len(self._records) >= self.capacity:
                self._records.pop(0)
                self._by_key = {k: index - 1 for index, k in enumerate(self._by_key)}
            self._by_key[key] = len(self._records)
            self._records.append(deepcopy(trace))
        return deepcopy(trace)

    def query(self, *, run_id: str | None = None, person_id: str | None = None,
              snapshot_id: str | None = None, time_seconds: float | None = None) -> list[dict[str, Any]]:
        rows = self._records
        if run_id is not None:
            rows = [row for row in rows if row["run_id"] == run_id]
        if person_id is not None:
            rows = [row for row in rows if row["person_id"] == person_id]
        if snapshot_id is not None:
            rows = [row for row in rows if row.get("snapshot_id") == snapshot_id]
        if time_seconds is not None:
            rows = [row for row in rows if row.get("time_seconds") == time_seconds]
        return deepcopy(rows)

    def latest(self, *, run_id: str | None = None, person_id: str | str | None = None) -> dict[str, Any] | None:
        rows = self.query(run_id=run_id, person_id=person_id)
        return rows[-1] if rows else None

    def persist(self, directory: str | Path) -> Path:
        """Append traces that are not yet persisted; returns the JSONL path."""
        path = Path(directory) / "decision_traces.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for trace in self._records:
                handle.write(json.dumps(trace, ensure_ascii=False, separators=(",", ":"),
                                        allow_nan=False, default=_json_default) + "\n")
        return path

    @staticmethod
    def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
        file = Path(path)
        if not file.is_file():
            return []
        rows = []
        for line in file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict) and row.get("version") == VERSION:
                rows.append(row)
        return rows


def _json_default(value: Any) -> Any:
    from dataclasses import asdict as _asdict
    if is_dataclass(value):
        return _asdict(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)
