"""Streaming incremental role-summary reducer over the archived JSONL.

The role summary must aggregate every boundary trace, but a multi-hundred-MB
archive cannot be fully decoded on every GET. This module folds each line once
into a compact deduplicated accumulator and caches that state per archive path,
keyed by the immutable file identity ``(size, mtime_ns)`` the same way the
byte index does:

- first scan: stream lines from byte zero, fold observations, plans, intervals,
  participation arms and the PAD/desire timeseries into accumulators;
- warm hit: reuse the folded accumulators, decoding none of the archive again;
- append-only growth: continue folding from the recorded tail offset; a
  truncated/rewritten file rebuilds from scratch.

The reducer stores no full trace body. It never writes the archive, the learner,
or any control collection. The produced structure is ``role-summary-v1`` and
matches :func:`crowdsim.infrastructure.role_summary.summarize_traces`.
"""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from crowdsim.infrastructure.archive_learning_gate import load_binding_truth, normalize_trace
from crowdsim.infrastructure.decision_trace import VERSION
from crowdsim.infrastructure.role_summary import (
    ACTIONS,
    VERSION as SUMMARY_VERSION,
    _arm_key,
    _blank_role,
    _finalize,
    _plan_key,
)

_LOCK = threading.RLock()
_CACHE: dict[str, dict[str, Any]] = {}
_BUILDING: set[str] = set()
_BUILD_LOCK = threading.Lock()


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _stat_identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _fresh_state(identity: tuple[int, int]) -> dict[str, Any]:
    return {
        "identity": identity,
        "next_offset": 0,
        "roles": {},
        "persons": {},
        "seen_plans": {},
        "seen_intervals": {},
        "participation_final": {},
        "protected_arms": {},
        "timeseries": {},
    }


def _fold_line(state: dict[str, Any], row: dict[str, Any]) -> None:
    person_id = row.get("person_id")
    if person_id is None:
        return
    role_id = row.get("role_id") or "unspecified"
    role = state["roles"].setdefault(role_id, _blank_role())

    # Latest-boundary learning is kept per person and attributed at materialize
    # time, mirroring the pure summary that reads the person's latest row.
    time_seconds = row.get("time_seconds")
    previous = state["persons"].get(person_id)
    if previous is None or (
        time_seconds is not None
        and (previous["latest_time"] is None or time_seconds >= previous["latest_time"])
    ):
        state["persons"][person_id] = {
            "role_id": role_id,
            "latest_time": time_seconds,
            "latest_learning": row.get("learning") or {},
        }

    observation = row.get("observation") or {}
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

    affect = row.get("affect") or {}
    if affect.get("status") != "evaluated":
        role["missing"]["affect_not_evaluated"] += 1

    plan = row.get("plan") or {}
    plan_key = _plan_key(plan, person_id)
    if plan_key not in state["seen_plans"].setdefault(role_id, set()):
        state["seen_plans"][role_id].add(plan_key)
        action = plan.get("proposed_action")
        role["actions"][action if action in ACTIONS else "unknown"] += 1
        if action == "wait":
            role["wait"]["plans"] += 1
            wait_until = plan.get("wait_until")
            if (isinstance(wait_until, (int, float)) and isinstance(time_seconds, (int, float))
                    and wait_until > time_seconds):
                role["wait"]["cumulative_hold_seconds"] += round(wait_until - time_seconds, 4)
        if action in ("reroute", "change_goal"):
            role["route"]["route_plans"] += 1
            entry = plan.get("selected_entry_edge") or plan.get("target_id") or plan.get("candidate_id")
            if entry is not None:
                role["entries"][str(entry)] += 1

    seen_intervals = state["seen_intervals"].setdefault(role_id, set())
    for event in row.get("learning", {}).get("events") or []:
        if event.get("kind") != "interval":
            continue
        interval_id = event.get("interval_id")
        if interval_id is not None:
            if str(interval_id) in seen_intervals:
                continue
            seen_intervals.add(str(interval_id))
            if event.get("trainable"):
                role["route"]["completed_route_evidence"] += 1
            exposure = event.get("risk_exposure_seconds")
            if isinstance(exposure, (int, float)):
                role["risk"]["risk_exposure_seconds"] += float(exposure)

    participation = state["participation_final"].setdefault(role_id, {})
    protected = state["protected_arms"].setdefault(role_id, set())
    for entry in row.get("participation") or []:
        arm_key = _arm_key(entry, person_id)
        participation[arm_key] = str(entry.get("state"))
        if entry.get("state") == "constraint_blocked" and entry.get("target_kind") in ("route", "local"):
            protected.add(arm_key)

    desire = affect.get("desire") or {}
    state["timeseries"].setdefault(role_id, []).append({
        "person_id": person_id,
        "time_seconds": time_seconds,
        "snapshot_id": row.get("snapshot_id"),
        "desire_primary": desire.get("primary"),
        "pad": affect.get("pad"),
    })


def _scan(path: Path, state: dict[str, Any]) -> None:
    truth = load_binding_truth(path.parent)
    with path.open("rb") as handle:
        if state["next_offset"] > 0:
            handle.seek(state["next_offset"])
        else:
            handle.seek(0)
        while True:
            line = handle.readline()
            if not line:
                break
            state["next_offset"] = handle.tell()
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("version") == VERSION:
                _fold_line(state, normalize_trace(row, truth))


def _materialize(state: dict[str, Any]) -> dict[str, Any]:
    # Start from the folded accumulators (observations, plans, intervals,
    # risk, entries), then layer per-person latest learning and arm finals on
    # top. A deep copy keeps the cached accumulators untouched.
    roles: dict[str, dict[str, Any]] = deepcopy(state["roles"])
    for role_id in state["roles"]:
        roles.setdefault(role_id, _blank_role())

    for person_id, person in state["persons"].items():
        role_id = person["role_id"] or "unspecified"
        role = roles.setdefault(role_id, _blank_role())
        role["population"]["persons"] += 1
        role["population"]["with_trace"] += 1
        latest_learning = person["latest_learning"] or {}
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

    for role_id, arms in state["participation_final"].items():
        role = roles.setdefault(role_id, _blank_role())
        for arm_key, arm_state in arms.items():
            if arm_state == "downstream_refined":
                role["learning"]["downstream_refined"] += 1
            elif arm_state == "participated_unchanged":
                role["learning"]["participated_unchanged"] += 1
            elif arm_state == "constraint_blocked":
                role["learning"]["constraint_blocked"] += 1
                if arm_key in state["protected_arms"].get(role_id, set()):
                    role["learning"]["protected_unchanged"] += 1

    for role_id, series in state["timeseries"].items():
        roles.setdefault(role_id, _blank_role())["affect_desire_timeseries"] = series

    finalized = {role_id: _finalize(role) for role_id, role in roles.items()}
    return {"version": SUMMARY_VERSION, "roles": finalized}


def summary_from_archive(path: str | Path) -> dict[str, Any]:
    """Return the cached role summary for the archive, folding new lines if any."""
    resolved = _resolve(path)
    key = str(resolved)
    with _LOCK:
        identity = _stat_identity(resolved)
        cached = _CACHE.get(key)
        if cached is not None:
            if cached["identity"] == identity:
                state = cached
            elif identity[0] >= cached["next_offset"] and cached["roles"]:
                cached["identity"] = identity
                state = cached
            else:
                state = _fresh_state(identity)
                _CACHE[key] = state
        else:
            state = _fresh_state(identity)
            _CACHE[key] = state
        _scan(resolved, state)
        return _materialize(deepcopy(state))


def cache_stats(path: str | Path) -> dict[str, Any]:
    """Report cache presence/identity for diagnostics; never reads the body."""
    resolved = _resolve(path)
    cached = _CACHE.get(str(resolved))
    if cached is None:
        return {"cached": False}
    return {
        "cached": True,
        "identity": list(cached["identity"]),
        "next_offset": cached["next_offset"],
        "persons": len(cached["persons"]),
        "roles": sorted(cached["roles"].keys()),
    }


def _ready(cached: dict[str, Any] | None, identity: tuple[int, int]) -> bool:
    return cached is not None and cached["identity"] == identity


def try_summary(path: str | Path) -> dict[str, Any] | None:
    """Return the summary only if a cache matching the current identity is ready.

    Never scans the archive. Returns ``None`` when the reducer is not built yet
    (or the file changed since the last build); pair with :func:`ensure_summary_async`.
    """
    resolved = _resolve(path)
    key = str(resolved)
    with _LOCK:
        identity = _stat_identity(resolved)
        cached = _CACHE.get(key)
        if _ready(cached, identity):
            return _materialize(deepcopy(cached))
    return None


def ensure_summary_async(path: str | Path) -> bool:
    """Trigger a background reducer build if not ready; return whether ready now.

    The first large archive scan runs on a daemon thread so the HTTP request
    does not block on a multi-second sync decode. Repeated calls while a build
    is in progress do not start a second builder.
    """
    resolved = _resolve(path)
    key = str(resolved)
    with _LOCK:
        identity = _stat_identity(resolved)
        if _ready(_CACHE.get(key), identity):
            return True
    with _BUILD_LOCK:
        if key in _BUILDING:
            return False
        _BUILDING.add(key)

    def _build() -> None:
        try:
            summary_from_archive(resolved)
        finally:
            with _BUILD_LOCK:
                _BUILDING.discard(key)

    threading.Thread(target=_build, daemon=True, name="archive-reducer-warm").start()
    return False
