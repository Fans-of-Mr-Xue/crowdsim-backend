"""History archive attribution gate and append-only event join tests.

These cover read-only projection over persisted v4-v6 style traces. The raw
``learning`` block is preserved under ``raw``; a personal-learner binding
proven from the archive sidecar is required before any accepted/update/selection
count enters a role summary. Events are joined from ``learning_runtime.jsonl``
by person, run, and boundary; cross-run events are never replayed onto a trace.
No SUMO, learner update, LLM call, or archive rewrite is performed here.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from crowdsim.infrastructure.decision_trace import VERSION as TRACE_VERSION
from crowdsim.infrastructure.decision_trace_archive import (
    archive_summary,
    load_archive_traces,
)
from crowdsim.infrastructure.decision_trace_reducer import summary_from_archive

RUN = "run-" + "a" * 32
FOREIGN_RUN = "run-" + "b" * 32


def _trace(pid: str, role: str, snapshot: str, time: float, learning: dict) -> dict:
    return {
        "version": TRACE_VERSION,
        "run_id": RUN,
        "person_id": pid,
        "role_id": role,
        "snapshot_id": snapshot,
        "time_seconds": time,
        "observation": {"status": "observed", "perceived_risk": 0.2, "perceived_crowding": 0.3},
        "affect": {"status": "evaluated", "desire": {"primary": "safety"}},
        "learning": learning,
        "plan": {"proposed_action": "continue"},
        "participation": [],
    }


def _write(root: Path, rows: list[dict], *, summary: dict | None = None,
           events: list[dict] | None = None) -> Path:
    directory = root / RUN
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "decision_traces.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8")
    if summary is not None:
        (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if events is not None:
        (directory / "learning_runtime.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return directory


def _summary(roles: dict, individuals: dict) -> dict:
    return {"run_id": RUN, "learning_controller": {"roles": roles, "individuals": individuals}}


def test_unbound_role_global_counts_excluded_from_summary():
    rows = [_trace("c1", "commuter", "r:1", 1.0, {
        "attached": True, "method_id": None, "accepted_samples": 35939,
        "update_version": 35939, "selections": 6872})]
    summary = _summary({}, {})
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), rows, summary=summary)
        traces, _ = load_archive_traces(RUN, root=tmp)
        block = traces[0]["learning"]

        assert block["attached"] is False
        assert block["accepted_samples"] == 0
        assert block["update_version"] == 0
        assert block["selections"] is None
        assert block["aggregation_excluded"] is True
        assert block["exclusion_reason"] == "no_personal_learner_binding"
        # Raw persisted block is retained for audit; its invalid counts are not
        # dropped from the archive file but are kept under ``raw`` only.
        assert block["raw"]["attached"] is True
        assert block["raw"]["accepted_samples"] == 35939
        assert block["raw"]["selections"] == 6872

        role = archive_summary(traces)["roles"]["commuter"]
        assert role["learning"]["accepted_samples"] == 0
        assert role["learning"]["updates"] == 0
        assert role["learning"]["selections"] == 0
        assert role["missing"]["learning_excluded_invalid"] == 1


def test_bound_individual_selection_is_per_person_not_global():
    rows = [_trace("s1", "social_visitor", "r:1", 1.0, {
        "attached": True, "method_id": "linucb_v1", "accepted_samples": 599,
        "update_version": 599, "selections": 6872})]
    summary = _summary(
        {"social_visitor": {"method_id": "linucb_v1", "interval_mode": "decision_interval"}},
        {"s1": {"method_id": "linucb_v1", "accepted_samples": 599, "update_version": 599,
                "binding_fingerprint": "sha256:s1", "frozen": False}})
    events = [
        {"run_id": RUN, "person_id": "s1", "kind": "selection", "snapshot_id": "r:1",
         "time_seconds": 1.0, "policy": "baseline_family_preserved",
         "baseline": {"proposed_action": "continue"}, "selected": {"proposed_action": "change_goal"},
         "scores": []},
        {"run_id": FOREIGN_RUN, "person_id": "s1", "kind": "selection", "snapshot_id": "r:1",
         "time_seconds": 1.0, "baseline": {}, "selected": {"proposed_action": "reroute"}},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), rows, summary=summary, events=events)
        traces, _ = load_archive_traces(RUN, root=tmp)
        block = traces[0]["learning"]

        assert block["attached"] is True
        assert block["accepted_samples"] == 599
        assert block["update_version"] == 599
        # Per-person selection comes from this run's events; the raw global 6872
        # and the foreign-run event are both kept out.
        assert block["selections"] == 1
        joined = [e for e in block["events"] if e["kind"] == "selection"]
        assert len(joined) == 1
        assert joined[0]["selected_action"] == "change_goal"

        role = archive_summary(traces)["roles"]["social_visitor"]
        assert role["learning"]["accepted_samples"] == 599
        assert role["learning"]["updates"] == 599
        assert role["learning"]["selections"] == 1
        assert role["missing"]["learning_excluded_invalid"] == 0


def test_events_joined_from_append_only_log_when_trace_body_empty():
    rows = [_trace("u2", "consumer", "r:1", 1.0, {
        "attached": True, "method_id": "bounded_adaptation_v1",
        "accepted_samples": 1, "update_version": 1, "selections": None, "events": []})]
    summary = _summary(
        {"consumer": {"method_id": "bounded_adaptation_v1", "interval_mode": "completed_route"}},
        {"u2": {"method_id": "bounded_adaptation_v1", "accepted_samples": 1, "update_version": 1,
                "binding_fingerprint": "sha256:u2", "frozen": False}})
    events = [{
        "run_id": RUN, "person_id": "u2", "kind": "learning_update", "snapshot_id": "r:1",
        "time_seconds": 1.0, "interval_id": "i1", "acceptance": "accepted",
        "sample": {"sample_id": "sample-1", "action": "continue", "target_id": "t1",
                   "candidate_id": "c1", "state": [0.1], "features": [0.2],
                   "next_state": [0.3], "reward": 0.0, "observed_cost_seconds": 4.0,
                   "cost_complete": True},
        "evaluation": {"reward": 0.0, "end_reason": "completed_route"},
        "updates": [],
    }]
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), rows, summary=summary, events=events)
        traces, _ = load_archive_traces(RUN, root=tmp)
        block = traces[0]["learning"]

        joined = [e for e in block["events"] if e["kind"] == "learning_update"]
        assert len(joined) == 1
        sample = joined[0]["accepted_sample"]
        assert sample["sample_id"] == "sample-1"
        assert sample["observed_cost_seconds"] == 4.0
        assert sample["cost_complete"] is True
        assert joined[0]["evaluation"]["end_reason"] == "completed_route"


def test_missing_sidecar_proof_keeps_counts_unavailable():
    rows = [_trace("x9", "social_visitor", "r:1", 1.0, {
        "attached": True, "method_id": "linucb_v1", "accepted_samples": 12,
        "update_version": 9, "selections": 4})]
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), rows)  # no summary.json, no learning_runtime.jsonl
        traces, _ = load_archive_traces(RUN, root=tmp)
        block = traces[0]["learning"]

        assert block["attached"] is False
        assert block["selections"] is None
        assert block["aggregation_excluded"] is True
        assert block["exclusion_reason"] == "binding_proof_unavailable"
        assert block["raw"]["accepted_samples"] == 12


def test_reducer_excludes_unbound_global_counts():
    rows = [
        _trace("c1", "commuter", "r:1", 1.0, {"attached": True, "method_id": None,
               "accepted_samples": 35939, "update_version": 35939, "selections": 6872}),
        _trace("s1", "social_visitor", "r:1", 1.0, {"attached": True, "method_id": "linucb_v1",
               "accepted_samples": 599, "update_version": 599, "selections": 3}),
    ]
    summary = _summary(
        {"social_visitor": {"method_id": "linucb_v1", "interval_mode": "decision_interval"}},
        {"s1": {"method_id": "linucb_v1", "accepted_samples": 599, "update_version": 599,
                "binding_fingerprint": "sha256:s1", "frozen": False}})
    events = [{"run_id": RUN, "person_id": "s1", "kind": "selection", "snapshot_id": "r:1",
               "time_seconds": 1.0, "baseline": {}, "selected": {}, "scores": []}]
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), rows, summary=summary, events=events)
        reducer = summary_from_archive(path / "decision_traces.jsonl")

        commuter = reducer["roles"]["commuter"]
        assert commuter["learning"]["accepted_samples"] == 0
        assert commuter["learning"]["updates"] == 0
        assert commuter["learning"]["selections"] == 0
        assert commuter["missing"]["learning_excluded_invalid"] == 1

        social = reducer["roles"]["social_visitor"]
        assert social["learning"]["accepted_samples"] == 599
        assert social["learning"]["updates"] == 599
        assert social["learning"]["selections"] == 1


def _interval_event(interval_id: str, pid: str, origin_snap: str, origin_time: float,
                     ended_at: float) -> dict:
    return {
        "kind": "interval", "run_id": RUN, "record_id": f"{RUN}:learning:iv-{interval_id}",
        "evidence": {
            "interval_id": interval_id, "person_id": pid, "role_id": "social_visitor",
            "started_at": origin_time, "ended_at": ended_at, "trainable": True,
            "plan": {"snapshot_id": origin_snap, "decided_at": origin_time, "person_id": pid},
            "receipt": {"applied_action": "continue", "applied_at": origin_time,
                        "person_id": pid, "status": "applied"},
        },
    }


def _update_event(interval_id: str, pid: str, after_snap: str, after_time: float,
                  origin_snap: str, origin_time: float, sample_id: str) -> dict:
    return {
        "kind": "learning_update", "run_id": RUN, "person_id": pid, "interval_id": interval_id,
        "snapshot_id": after_snap, "time_seconds": after_time, "acceptance": "accepted",
        "before_encoding": {"snapshot_id": origin_snap, "time_seconds": origin_time},
        "sample": {"sample_id": sample_id, "action": "continue", "target_id": "t1",
                   "candidate_id": "c1", "state": [0.1], "features": [0.2],
                   "next_state": [0.3], "reward": 0.0, "observed_cost_seconds": 4.0,
                   "cost_complete": True, "evidence_id": interval_id},
        "evaluation": {"reward": 0.0, "end_reason": "completed_route", "evidence_id": interval_id},
        "updates": [],
    }


def test_history_frame_counts_keep_raw_timeline_not_final_proof():
    rows = [
        _trace("h1", "social_visitor", "r:1", 1.0, {
            "attached": True, "method_id": "linucb_v1", "accepted_samples": 0,
            "update_version": 0, "selections": 0}),
        _trace("h1", "social_visitor", "r:3", 3.0, {
            "attached": True, "method_id": "linucb_v1", "accepted_samples": 1,
            "update_version": 1, "selections": 1}),
    ]
    # Proof holds the final archive total; it must not be back-filled onto the
    # earlier frame, which would fabricate a first-frame final count.
    summary = _summary(
        {"social_visitor": {"method_id": "linucb_v1", "interval_mode": "decision_interval"}},
        {"h1": {"method_id": "linucb_v1", "accepted_samples": 599, "update_version": 599,
                "binding_fingerprint": "sha256:h1", "frozen": False}})
    events = [
        {"run_id": RUN, "person_id": "h1", "kind": "selection", "snapshot_id": "r:3",
         "time_seconds": 3.0, "baseline": {"snapshot_id": "r:1"}, "selected": {"snapshot_id": "r:3"},
         "record_id": f"{RUN}:learning:sel-1", "scores": []},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), rows, summary=summary, events=events)
        traces, _ = load_archive_traces(RUN, root=tmp)
        by_time = {t["time_seconds"]: t["learning"] for t in traces}

        first = by_time[1.0]
        assert first["accepted_samples"] == 0
        assert first["update_version"] == 0
        assert first["selections"] == 0  # selection at 3.0 is not counted yet

        last = by_time[3.0]
        assert last["accepted_samples"] == 1  # recorded timeline, not proof final 599
        assert last["update_version"] == 1
        assert last["selections"] == 1


def test_bounded_zero_is_not_backfilled_from_proof():
    rows = [_trace("b1", "consumer", "r:1", 1.0, {
        "attached": True, "method_id": "bounded_adaptation_v1",
        "accepted_samples": 0, "update_version": 0, "selections": None})]
    summary = _summary(
        {"consumer": {"method_id": "bounded_adaptation_v1", "interval_mode": "completed_route"}},
        {"b1": {"method_id": "bounded_adaptation_v1", "accepted_samples": 0, "update_version": 0,
                "binding_fingerprint": "sha256:b1", "frozen": False}})
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), rows, summary=summary, events=[])
        traces, _ = load_archive_traces(RUN, root=tmp)
        block = traces[0]["learning"]

        assert block["attached"] is True
        assert block["accepted_samples"] == 0
        assert block["update_version"] == 0
        assert block["selections"] == 0
        assert block["linked_intervals"] == []


def test_cross_boundary_interval_linked_not_inserted_in_origin_events():
    rows = [
        _trace("k1", "social_visitor", "r:1", 1.0, {
            "attached": True, "method_id": "linucb_v1", "accepted_samples": 0,
            "update_version": 0, "selections": 0}),
        _trace("k1", "social_visitor", "r:3", 3.0, {
            "attached": True, "method_id": "linucb_v1", "accepted_samples": 1,
            "update_version": 1, "selections": 0}),
    ]
    summary = _summary(
        {"social_visitor": {"method_id": "linucb_v1", "interval_mode": "decision_interval"}},
        {"k1": {"method_id": "linucb_v1", "accepted_samples": 1, "update_version": 1,
                "binding_fingerprint": "sha256:k1", "frozen": False}})
    events = [
        _interval_event("i1", "k1", "r:1", 1.0, 3.0),
        _update_event("i1", "k1", "r:3", 3.0, "r:1", 1.0, "sample-k1"),
        {"run_id": RUN, "person_id": "k1", "kind": "selection", "snapshot_id": "r:5",
         "time_seconds": 5.0, "baseline": {"snapshot_id": "r:3"}, "selected": {"snapshot_id": "r:5"},
         "record_id": f"{RUN}:learning:sel-next", "scores": []},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), rows, summary=summary, events=events)
        traces, _ = load_archive_traces(RUN, root=tmp)
        by_time = {t["time_seconds"]: t for t in traces}

        origin = by_time[1.0]["learning"]
        # The terminal sample produced at 3.0 must not be placed on the origin
        # boundary's same-time events list.
        assert not any(e["kind"] == "learning_update" for e in origin["events"])
        assert len(origin["linked_intervals"]) == 1
        link = origin["linked_intervals"][0]
        assert link["interval_id"] == "i1"
        assert link["cross_boundary"] is True
        assert link["origin_decision_key"] == {"snapshot_id": "r:1", "time_seconds": 1.0}
        assert link["observed_after_at"] == {"snapshot_id": "r:3", "time_seconds": 3.0,
                                            "phase": "feedback_after"}
        assert link["next_selection_key"]["time_seconds"] == 5.0
        assert link["accepted_sample"]["sample_id"] == "sample-k1"
        assert link["source_timestamps"] == {"started_at": 1.0, "ended_at": 3.0}

        terminal = by_time[3.0]["learning"]
        assert any(e["kind"] == "learning_update" for e in terminal["events"])
        # The origin-bound interval is not re-linked from the terminal frame.
        assert all(li["interval_id"] != "i1" for li in terminal["linked_intervals"])


def test_events_joined_only_for_same_person_at_shared_boundary():
    rows = [
        _trace("pA", "social_visitor", "r:1", 1.0, {
            "attached": True, "method_id": "linucb_v1", "accepted_samples": 0,
            "update_version": 0, "selections": 0}),
        _trace("pB", "social_visitor", "r:1", 1.0, {
            "attached": True, "method_id": "linucb_v1", "accepted_samples": 0,
            "update_version": 0, "selections": 0}),
    ]
    summary = _summary(
        {"social_visitor": {"method_id": "linucb_v1", "interval_mode": "decision_interval"}},
        {"pA": {"method_id": "linucb_v1", "accepted_samples": 0, "update_version": 0,
                "binding_fingerprint": "sha256:pA", "frozen": False},
         "pB": {"method_id": "linucb_v1", "accepted_samples": 0, "update_version": 0,
                "binding_fingerprint": "sha256:pB", "frozen": False}})
    events = [
        {"run_id": RUN, "person_id": "pA", "kind": "selection", "snapshot_id": "r:1",
         "time_seconds": 1.0, "baseline": {}, "selected": {"proposed_action": "change_goal"},
         "scores": []},
        {"run_id": RUN, "person_id": "pB", "kind": "selection", "snapshot_id": "r:1",
         "time_seconds": 1.0, "baseline": {}, "selected": {"proposed_action": "reroute"},
         "scores": []},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp), rows, summary=summary, events=events)
        traces, _ = load_archive_traces(RUN, root=tmp)
        by_person = {t["person_id"]: t["learning"] for t in traces}

        a_selected = [e["selected_action"] for e in by_person["pA"]["events"]
                       if e["kind"] == "selection"]
        b_selected = [e["selected_action"] for e in by_person["pB"]["events"]
                      if e["kind"] == "selection"]
        # Shared snapshot/time, but each boundary joins only its own person's
        # event; the other person's selection is never projected across.
        assert a_selected == ["change_goal"]
        assert b_selected == ["reroute"]
        assert by_person["pA"]["selections"] == 1
        assert by_person["pB"]["selections"] == 1
