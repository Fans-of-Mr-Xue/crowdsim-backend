"""Read-only learning attribution gate for archived decision traces.

Older archives were projected before the binding-attribution fix: a person
without a personal learner could be flagged ``attached=true`` and carry the
controller-global accepted/update/selection totals. This module re-derives the
learning attribution from same-archive evidence (``summary.json`` individual
bindings and ``learning_runtime.jsonl`` per-person selection events) without
rewriting the persisted JSONL.

The raw block is retained under ``raw`` so actual recorded samples are not
dropped; only counts lacking a binding proof are excluded from role
aggregation. Controller-global selection totals are never copied onto a person.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from crowdsim.infrastructure.decision_trace import _learning_event

SUMMARY_FILE = "summary.json"
RUNTIME_EVENTS_FILE = "learning_runtime.jsonl"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _events_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


_TRUTH_CACHE: dict[str, dict[str, Any]] = {}


def load_binding_truth(runtime_dir: str | Path) -> dict[str, Any]:
    """Build per-person binding proof from read-only archive sidecar files.

    Returns a dict::

        {"available": bool,
         "individuals": {pid: {"method_id", "accepted_samples", "update_version",
                               "binding_fingerprint", "frozen"}},
         "role_methods": {role: method_id},
         "selection_by_person": {pid: int}}

    When ``summary.json`` is absent or unparseable ``available`` is False and
    every binding/selection value must be treated as unproven rather than
    inherited from a raw trace block. Results are cached on the sidecar file
    identity so a large events file is not rescanned per request.
    """
    directory = Path(runtime_dir)
    summary_path = directory / SUMMARY_FILE
    events_path = directory / RUNTIME_EVENTS_FILE
    identity = (str(directory), _events_identity(summary_path), _events_identity(events_path))
    cached = _TRUTH_CACHE.get(identity)
    if cached is not None:
        return cached

    summary = _read_json(summary_path)
    archive_run_id = summary.get("run_id") if isinstance(summary, dict) else None
    individuals: dict[str, Any] = {}
    role_methods: dict[str, str] = {}
    available = False
    if summary is not None:
        controller = summary.get("learning_controller") or {}
        for pid, item in (controller.get("individuals") or {}).items():
            if not isinstance(item, dict):
                continue
            individuals[str(pid)] = {
                "method_id": item.get("method_id"),
                "accepted_samples": item.get("accepted_samples") or 0,
                "update_version": item.get("update_version") or 0,
                "binding_fingerprint": item.get("binding_fingerprint"),
                "frozen": item.get("frozen"),
            }
        for role, cfg in (controller.get("roles") or {}).items():
            method = None
            if isinstance(cfg, dict):
                method = cfg.get("method_id")
                if method is None:
                    method = (cfg.get("setting") or {}).get("learning_config", {}).get("method_id")
            if method:
                role_methods[str(role)] = method
        available = True

    selection_by_person: dict[str, int] = {}
    selections_by_person: dict[str, list[dict[str, Any]]] = {}
    intervals: dict[str, dict[str, Any]] = {}
    updates_by_interval: dict[str, dict[str, Any]] = {}
    events_by_person: dict[Any, list[dict[str, Any]]] = {}
    intervals_by_person: dict[str, list[dict[str, Any]]] = {}
    if events_path.is_file():
        try:
            with events_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    # Keep events inside this archive run only; cross-run events
                    # must not be replayed into this person's attribution.
                    if archive_run_id and row.get("run_id") and row.get("run_id") != archive_run_id:
                        continue
                    pid = row.get("person_id")
                    if pid is None and isinstance(row.get("evidence"), dict):
                        pid = row["evidence"].get("person_id")
                    projected = _learning_event(pid, row)
                    if projected is None:
                        continue
                    # Retain only the match keys alongside the whitelisted
                    # projection; the raw large nested dict is not retained.
                    person_key = str(pid) if pid is not None else None
                    projected["person_id"] = person_key
                    projected["run_id"] = row.get("run_id")
                    events_by_person.setdefault(person_key, []).append(projected)
                    if pid is None:
                        continue
                    key = person_key
                    kind = projected["kind"]
                    if kind == "selection":
                        selections_by_person.setdefault(key, []).append(projected)
                    elif kind == "interval" and projected.get("interval_id"):
                        intervals[projected["interval_id"]] = projected
                        intervals_by_person.setdefault(key, []).append(projected)
                    elif kind == "learning_update" and projected.get("interval_id"):
                        updates_by_interval[projected["interval_id"]] = projected
            for key, sel_list in selections_by_person.items():
                sel_list.sort(key=lambda item: (item.get("time_seconds") is None,
                                                item.get("time_seconds") or 0.0))
                selection_by_person[key] = len(sel_list)
        except OSError:
            events_by_person = {}
            intervals_by_person = {}
            selection_by_person = {}
            selections_by_person = {}
            intervals = {}
            updates_by_interval = {}

    truth: dict[str, Any] = {
        "available": available,
        "run_id": archive_run_id,
        "individuals": individuals,
        "role_methods": role_methods,
        "selection_by_person": selection_by_person,
        "selections_by_person": selections_by_person,
        "intervals": intervals,
        "updates_by_interval": updates_by_interval,
        "events_by_person": events_by_person,
        "intervals_by_person": intervals_by_person,
    }
    _TRUTH_CACHE[identity] = truth
    return truth


def _event_matches_boundary(event: dict[str, Any], snapshot_id: Any, time_seconds: Any) -> bool:
    if snapshot_id is not None and event.get("snapshot_id") == snapshot_id:
        return True
    ev_time = event.get("time_seconds")
    evaluation = event.get("evaluation")
    if ev_time is None and isinstance(evaluation, dict):
        ev_time = evaluation.get("time_seconds")
    if isinstance(ev_time, (int, float)) and isinstance(time_seconds, (int, float)):
        return abs(float(ev_time) - float(time_seconds)) <= 1e-6
    return False


def join_boundary_events(trace: dict[str, Any], truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Join the append-only learning event log to one boundary trace.

    Events are attributed only to the same person in the same run; a snapshot id
    (or an equal boundary time) ties an event to this boundary. Cross-run events
    are never replayed onto this trace.
    """
    pid = trace.get("person_id")
    run_id = trace.get("run_id")
    snapshot_id = trace.get("snapshot_id")
    time_seconds = trace.get("time_seconds")
    person_key = str(pid) if pid is not None else None
    matched: list[dict[str, Any]] = []
    for event in (truth.get("events_by_person") or {}).get(person_key, []):
        if event.get("person_id") != person_key:
            continue
        if run_id and event.get("run_id") and event.get("run_id") != run_id:
            continue
        if not _event_matches_boundary(event, snapshot_id, time_seconds):
            continue
        projected = {key: value for key, value in event.items()
                     if key not in ("person_id", "run_id")}
        matched.append(projected)
    return matched


def _time_le(value: Any, bound: Any) -> bool:
    return (isinstance(value, (int, float)) and isinstance(bound, (int, float))
            and float(value) <= float(bound) + 1e-6)


def _selections_up_to(pid: Any, trace_time: Any, truth: dict[str, Any]) -> int | None:
    """Per-person selections recorded at or before this boundary time.

    The final archive total is not projected onto every historical frame; the
    count grows with the person's own selection events up to the trace time.
    """
    if trace_time is None or pid is None:
        return None
    return sum(1 for item in truth.get("selections_by_person", {}).get(str(pid), [])
               if _time_le(item.get("time_seconds"), trace_time))


def join_linked_intervals(trace: dict[str, Any], truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Read-only cross-boundary link: origin decision -> feedback-after sample -> next choice.

    A completed-route terminal sample can be produced on a later boundary than
    the origin decision. It is reported under ``linked_intervals`` with its real
    event timestamps, never inserted into the origin boundary's same-time
    ``events``. Cross-run intervals are excluded.
    """
    pid = trace.get("person_id")
    run_id = trace.get("run_id")
    snapshot_id = trace.get("snapshot_id")
    time_seconds = trace.get("time_seconds")
    person_key = str(pid) if pid is not None else None
    selections = truth.get("selections_by_person", {}).get(person_key, [])
    linked: list[dict[str, Any]] = []

    for interval in (truth.get("intervals_by_person") or {}).get(person_key, []):
        if interval.get("person_id") != person_key:
            continue
        if run_id and interval.get("run_id") and interval.get("run_id") != run_id:
            continue
        origin_snap = interval.get("origin_snapshot_id")
        origin_time = interval.get("origin_time_seconds")
        origin_match = (snapshot_id is not None and origin_snap == snapshot_id) or (
            isinstance(origin_time, (int, float)) and isinstance(time_seconds, (int, float))
            and abs(float(origin_time) - float(time_seconds)) <= 1e-6)
        if not origin_match:
            continue

        interval_id = interval.get("interval_id")
        update = truth.get("updates_by_interval", {}).get(interval_id)
        after_time = (update.get("after_time_seconds") if update else None) or interval.get("ended_at")
        after_snap = update.get("after_snapshot_id") if update else None

        next_selection = None
        for selection in selections:
            sel_time = selection.get("time_seconds")
            if isinstance(sel_time, (int, float)) and isinstance(after_time, (int, float)) \
                    and float(sel_time) > float(after_time) + 1e-6:
                next_selection = selection
                break

        linked.append({
            "interval_id": interval_id,
            "cross_boundary": isinstance(after_time, (int, float)) and isinstance(origin_time, (int, float))
                and float(after_time) > float(origin_time) + 1e-6,
            "origin_decision_key": {"snapshot_id": origin_snap, "time_seconds": origin_time},
            "observed_after_at": {"snapshot_id": after_snap, "time_seconds": after_time,
                                  "phase": "feedback_after"},
            "next_selection_key": None if next_selection is None else {
                "record_id": next_selection.get("record_id"),
                "snapshot_id": next_selection.get("selected_snapshot_id") or next_selection.get("snapshot_id"),
                "time_seconds": next_selection.get("time_seconds"),
            },
            "accepted_sample": update.get("accepted_sample") if update else None,
            "source_timestamps": {"started_at": interval.get("started_at"),
                                  "ended_at": interval.get("ended_at")},
            "stage": "feedback_after",
        })
    return linked


def normalize_learning_block(trace: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    """Re-project one archived trace's learning block with binding proof.

    The persisted raw block is copied under ``raw``. Counts are retained only
    when a matching personal-learner binding exists in the archive truth;
    otherwise the block is marked ``aggregation_excluded`` with a reason and its
    counts are zeroed/unset for summary attribution.
    """
    pid = trace.get("person_id")
    role = trace.get("role_id")
    raw: dict[str, Any] = deepcopy(trace.get("learning") or {})
    individuals = truth.get("individuals") or {}
    selection_by_person = truth.get("selection_by_person") or {}
    role_methods = truth.get("role_methods") or {}

    # Older archives persisted no per-boundary event bodies in the trace file;
    # join the append-only learning log for this same person/run/boundary.
    joined_events = join_boundary_events(trace, truth)
    events_out = raw.get("events") or joined_events
    linked_intervals = join_linked_intervals(trace, truth)

    proof = individuals.get(str(pid)) if pid is not None else None
    role_method = role_methods.get(role)

    def _excluded(reason: str, method_id: Any) -> dict[str, Any]:
        return {
            "attached": False,
            "method_id": method_id,
            "accepted_samples": 0,
            "update_version": 0,
            "selections": None,
            "binding": None,
            "pending_or_replay_samples": 0,
            "events": events_out,
            "linked_intervals": [],
            "source_invalid_counts": True,
            "aggregation_excluded": True,
            "exclusion_reason": reason,
            "raw": raw,
        }

    if proof is None:
        # No personal learner in archive truth. When proof itself is
        # unavailable the reason is kept distinct from a confirmed unbound role.
        reason = "binding_proof_unavailable" if not truth.get("available") else "no_personal_learner_binding"
        return _excluded(reason, raw.get("method_id") or role_method)

    proof_method = proof.get("method_id")
    if role_method is not None and proof_method is not None and proof_method != role_method:
        return _excluded("binding_method_mismatch", proof_method)

    def _raw_number(key: str) -> int | None:
        value = raw.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    # Binding proof validates identity only. The per-boundary accepted/update
    # counters keep their recorded historical time series; the final archive
    # total must not be back-filled onto earlier frames (that would fabricate a
    # first-frame final count). Missing a recorded value falls back to 0 rather
    # than to the proof final count.
    raw_accepted = _raw_number("accepted_samples")
    raw_update = _raw_number("update_version")
    # Selections grow from this person's own events up to this boundary time;
    # the final per-person total is not projected onto every frame.
    selections = _selections_up_to(pid, trace.get("time_seconds"), truth)
    invalid_counts = bool(raw.get("attached")) and (
        raw.get("selections") is not None and str(pid) not in selection_by_person
    )

    return {
        "attached": True,
        "method_id": proof_method or raw.get("method_id"),
        "accepted_samples": raw_accepted or 0,
        "update_version": raw_update or 0,
        "selections": selections,
        "pending_or_replay_samples": raw.get("pending_or_replay_samples") or 0,
        "binding": {
            "binding_fingerprint": proof.get("binding_fingerprint"),
            "frozen": proof.get("frozen"),
        },
        "events": events_out,
        "linked_intervals": linked_intervals,
        "source_invalid_counts": invalid_counts,
        "aggregation_excluded": False,
        "exclusion_reason": None,
        "raw": raw,
    }


def normalize_trace(trace: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the trace with its learning block re-projected."""
    projected = deepcopy(trace)
    projected["learning"] = normalize_learning_block(trace, truth)
    return projected
