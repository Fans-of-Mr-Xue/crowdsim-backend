"""Per-role effect aggregation from confirmed decision traces.

The summary counts only what the traces record. Every aggregate keeps an
explicit denominator and a ``missing`` breakdown; protected arms that were
kept unchanged are counted separately rather than folded into learning impact.
No narrative stitching, no extrapolated natural reward, no per-seed or
thousand-person expansion.

Aggregated dimensions per role:

- ``actions``: applied/proposed action counts from the final plan.
- ``entries``: supplied candidate/entry selected counts.
- ``wait``: wait plans and cumulative planned hold interval.
- ``route``: route-family plans and completed-route interval evidence.
- ``risk``: average perceived risk/crowding over observed traces.
- ``affect_desire_timeseries``: ordered per-trace PAD/desire-primary references.
- ``learning``: accepted samples, updates, selections, and participation
  states (downstream refined / unchanged / constraint blocked / protected).
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any, Iterable

VERSION = "role-summary-v1"
ACTIONS = ("continue", "slow_down", "wait", "reroute", "change_goal")


def _blank_role() -> dict[str, Any]:
    return {
        "population": {"persons": 0, "with_trace": 0},
        "missing": {"no_trace": 0, "no_observation": 0, "affect_not_evaluated": 0,
                    "learning_not_attached": 0, "learning_excluded_invalid": 0},
        "actions": {action: 0 for action in (*ACTIONS, "unknown")},
        "entries": defaultdict(int),
        "wait": {"plans": 0, "cumulative_hold_seconds": 0.0},
        "route": {"route_plans": 0, "completed_route_evidence": 0},
        "risk": {"perceived_risk_sum": 0.0, "perceived_crowding_sum": 0.0, "observations": 0,
                 "risk_exposure_seconds": 0.0},
        "affect_desire_timeseries": [],
        "learning": {"accepted_samples": 0, "updates": 0, "selections": 0,
                     "downstream_refined": 0, "participated_unchanged": 0,
                     "constraint_blocked": 0, "protected_unchanged": 0},
    }


def _arm_key(entry: dict[str, Any], person_id: str) -> tuple[Any, ...]:
    """Stable identity of one participation arm across repeated frames."""
    return (str(person_id), entry.get("action"), entry.get("candidate_id"),
            entry.get("target_id"), entry.get("target_kind"))


def _plan_key(plan: dict[str, Any], person_id: str) -> tuple[Any, ...]:
    """Stable identity of one persisted plan; repeated frames of the same plan count once."""
    return (
        str(person_id),
        plan.get("proposed_action"),
        plan.get("target_id"),
        plan.get("candidate_id"),
        tuple(plan.get("route_edges") or ()),
        plan.get("arrival_position"),
        plan.get("wait_until"),
        plan.get("activity_duration"),
    )


def _finalize(role: dict[str, Any]) -> dict[str, Any]:
    observations = role["risk"]["observations"]
    role["risk"] = {
        "avg_perceived_risk": round(role["risk"]["perceived_risk_sum"] / observations, 4) if observations else None,
        "avg_perceived_crowding": round(role["risk"]["perceived_crowding_sum"] / observations, 4) if observations else None,
        "observations": observations,
        "risk_exposure_seconds": round(role["risk"]["risk_exposure_seconds"], 4),
        "basis": "averages over boundary traces; exposure seconds summed over distinct completed intervals",
    }
    role["entries"] = dict(sorted(role["entries"].items()))
    role["affect_desire_timeseries"].sort(key=lambda row: (row["time_seconds"], row["person_id"]))
    role["basis"] = {
        "actions": "distinct persisted plans counted once by plan identity across repeated frames",
        "wait": "wait plans and hold seconds counted once per distinct wait plan",
        "route": "route plans and entries counted once per distinct route plan",
        "participation": "one record per arm; terminal observed state kept over repeated frames",
    }
    return role


def summarize_traces(traces: Iterable[dict[str, Any]], *, population_persons: dict[str, str] | None = None) -> dict[str, Any]:
    """Aggregate traces by role.

    ``population_persons`` optionally maps person_id -> role_id so persons
    without a trace are still counted in the denominator as ``no_trace``.

    Cumulative learning counters are read once per person from that person's
    latest boundary trace (not summed over repeated frames). Interval evidence,
    selections and participation records are de-duplicated by identity so the
    same event repeated across frames does not inflate the denominator.
    """
    roles: dict[str, dict[str, Any]] = defaultdict(_blank_role)
    population_persons = population_persons or {}

    for person_id, role_id in population_persons.items():
        roles[role_id]["population"]["persons"] += 1

    rows_by_person: dict[str, list[dict[str, Any]]] = {}
    for trace in traces:
        if not isinstance(trace, dict) or trace.get("version") != "decision-trace-v1":
            continue
        pid = trace.get("person_id")
        rows_by_person.setdefault(pid, []).append(trace)

    seen_intervals: dict[str, set[str]] = defaultdict(set)
    seen_plans: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    participation_final: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)
    protected_arms: dict[str, set[tuple[str, str]]] = defaultdict(set)

    for pid, rows in rows_by_person.items():
        rows.sort(key=lambda item: item.get("time_seconds") or 0)
        latest = rows[-1]
        role_id = latest.get("role_id") or "unspecified"
        role = roles[role_id]
        if pid not in population_persons:
            role["population"]["persons"] += 1
        role["population"]["with_trace"] += 1

        latest_learning = latest.get("learning") or {}
        if latest_learning.get("attached") and not latest_learning.get("aggregation_excluded"):
            role["learning"]["accepted_samples"] += int(latest_learning.get("accepted_samples") or 0)
            role["learning"]["updates"] += int(latest_learning.get("update_version") or 0)
            selections = latest_learning.get("selections")
            if isinstance(selections, (int, float)):
                role["learning"]["selections"] += int(selections)
        else:
            role["missing"]["learning_not_attached"] += 1
            if latest_learning.get("aggregation_excluded"):
                role["missing"]["learning_excluded_invalid"] += 1

        for trace in rows:
            observation = trace.get("observation") or {}
            if observation.get("status") != "observed":
                role["missing"]["no_observation"] += 1
            else:
                risk = observation.get("perceived_risk")
                crowding = observation.get("perceived_crowding")
                if isinstance(risk, (int, float)):
                    role["risk"]["perceived_risk_sum"] += risk
                if isinstance(crowding, (int, float)):
                    role["risk"]["perceived_crowding_sum"] += crowding
                role["risk"]["observations"] += 1

            affect = trace.get("affect") or {}
            if affect.get("status") != "evaluated":
                role["missing"]["affect_not_evaluated"] += 1

            learning = trace.get("learning") or {}
            plan = trace.get("plan") or {}
            plan_key = _plan_key(plan, pid)
            if plan_key not in seen_plans[role_id]:
                seen_plans[role_id].add(plan_key)
                action = plan.get("proposed_action")
                role["actions"][action if action in ACTIONS else "unknown"] += 1
                if action == "wait":
                    role["wait"]["plans"] += 1
                    wait_until = plan.get("wait_until")
                    decided_at = trace.get("time_seconds")
                    if isinstance(wait_until, (int, float)) and isinstance(decided_at, (int, float)) and wait_until > decided_at:
                        role["wait"]["cumulative_hold_seconds"] += round(wait_until - decided_at, 4)
                if action in ("reroute", "change_goal"):
                    role["route"]["route_plans"] += 1
                    entry = plan.get("selected_entry_edge") or plan.get("target_id") or plan.get("candidate_id")
                    if entry is not None:
                        role["entries"][str(entry)] += 1

            for event in learning.get("events") or []:
                if event.get("kind") != "interval":
                    continue
                interval_id = event.get("interval_id")
                if interval_id is not None and interval_id in seen_intervals[role_id]:
                    continue
                if interval_id is not None:
                    seen_intervals[role_id].add(str(interval_id))
                if event.get("trainable"):
                    role["route"]["completed_route_evidence"] += 1
                exposure = event.get("risk_exposure_seconds")
                if isinstance(exposure, (int, float)):
                    role["risk"]["risk_exposure_seconds"] += float(exposure)

            for entry in trace.get("participation") or []:
                state = entry.get("state")
                arm_key = _arm_key(entry, pid)
                # Same arm keeps its latest observed state; an unchanged->refined
                # transition is retained as the terminal state, not collapsed away.
                participation_final[role_id][arm_key] = str(state)
                if state == "constraint_blocked" and entry.get("target_kind") in ("route", "local"):
                    protected_arms[role_id].add(arm_key)

            desire = affect.get("desire") or {}
            role["affect_desire_timeseries"].append({
                "person_id": pid,
                "time_seconds": trace.get("time_seconds"),
                "snapshot_id": trace.get("snapshot_id"),
                "desire_primary": desire.get("primary"),
                "pad": affect.get("pad"),
            })

    for role_id, arm_states in participation_final.items():
        role = roles[role_id]
        for arm_key, state in arm_states.items():
            if state == "downstream_refined":
                role["learning"]["downstream_refined"] += 1
            elif state == "participated_unchanged":
                role["learning"]["participated_unchanged"] += 1
            elif state == "constraint_blocked":
                role["learning"]["constraint_blocked"] += 1
                if arm_key in protected_arms[role_id]:
                    role["learning"]["protected_unchanged"] += 1

    finalized = {role_id: _finalize(role) for role_id, role in roles.items()}
    return {"version": VERSION, "roles": finalized}
