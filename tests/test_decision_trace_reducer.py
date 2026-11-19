"""Streaming reducer equivalence with the pure summarize_traces summary.

The reducer folds the same archived rows the pure function consumes; this test
writes a small temporary JSONL, runs the reducer cache, and compares the
role-summary-v1 output field by field. No SUMO, learner update, or archive
write beyond the temporary fixture.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from crowdsim.infrastructure.archive_learning_gate import load_binding_truth, normalize_trace
from crowdsim.infrastructure.decision_trace import VERSION as TRACE_VERSION
from crowdsim.infrastructure.decision_trace_reducer import (
    ensure_summary_async,
    summary_from_archive,
    cache_stats,
    try_summary,
)
from crowdsim.infrastructure.role_summary import summarize_traces, VERSION as SUMMARY_VERSION


RUNTIME_ID = "run-" + "d" * 32


def _row(person, role, snapshot, time, *, action="continue", plan=None,
         participation=None, learning=None, observation="observed",
         affect="evaluated"):
    obs = {"status": observation, "perceived_risk": 0.2, "perceived_crowding": 0.3}
    if observation != "observed":
        obs = {"status": "missing_observation"}
    return {
        "version": TRACE_VERSION,
        "run_id": RUNTIME_ID,
        "person_id": person,
        "role_id": role,
        "snapshot_id": snapshot,
        "time_seconds": time,
        "observation": obs,
        "affect": {"status": affect, "desire": {"primary": "safety" if affect == "evaluated" else None}},
        "learning": learning or {"attached": False},
        "plan": plan or {"proposed_action": action},
        "participation": participation or [],
    }


def _fixture_rows() -> list[dict]:
    return [
        _row("p1", "consumer", "r:1", 1.0,
             learning={"attached": True, "accepted_samples": 2, "update_version": 1, "selections": 3},
             participation=[{"action": "continue", "candidate_id": "c1", "target_id": "t1",
                             "target_kind": "local", "state": "downstream_refined"}]),
        _row("p2", "consumer", "r:1", 1.0, action="wait",
             plan={"proposed_action": "wait", "wait_until": 3.0},
             participation=[{"action": "wait", "candidate_id": "c2", "target_id": "t2",
                             "target_kind": "route", "state": "constraint_blocked"}]),
        _row("p1", "consumer", "r:2", 2.0, action="reroute",
             plan={"proposed_action": "reroute", "selected_entry_edge": "E1", "target_id": "E1"},
             learning={"attached": True, "accepted_samples": 2, "update_version": 1, "selections": 3,
                       "events": [{"kind": "interval", "interval_id": "i1", "trainable": True,
                                   "risk_exposure_seconds": 1.5}]},
             participation=[{"action": "reroute", "candidate_id": "c3", "target_id": "E1",
                             "target_kind": "route", "state": "participated_unchanged"}]),
        _row("p3", "visitor", "r:1", 1.0, observation="missing", affect="missing"),
    ]


def _write(root: Path, rows: list[dict]) -> Path:
    directory = root / RUNTIME_ID
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "decision_traces.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    # Binding proof sidecar: p1/p2 are genuinely bound consumers; p1 has one
    # real per-person selection event in the append-only learning log.
    summary = {
        "learning_controller": {
            "roles": {"consumer": {"method_id": "bounded_adaptation_v1",
                                   "interval_mode": "completed_route"}},
            "individuals": {
                "p1": {"method_id": "bounded_adaptation_v1", "accepted_samples": 2,
                       "update_version": 1, "binding_fingerprint": "sha256:p1", "frozen": False},
                "p2": {"method_id": "bounded_adaptation_v1", "accepted_samples": 0,
                       "update_version": 0, "binding_fingerprint": "sha256:p2", "frozen": False},
            },
        }
    }
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    selection = {"run_id": RUNTIME_ID, "person_id": "p1", "kind": "selection",
                 "snapshot_id": "r:1", "time_seconds": 1.0, "policy": "baseline_family_preserved",
                 "baseline": {}, "selected": {}, "scores": []}
    (directory / "learning_runtime.jsonl").write_text(json.dumps(selection) + "\n", encoding="utf-8")
    return path


def _compare_roles(reducer: dict, pure: dict) -> None:
    assert reducer["version"] == SUMMARY_VERSION == pure["version"]
    assert set(reducer["roles"].keys()) == set(pure["roles"].keys())
    for role_id, pure_role in pure["roles"].items():
        red_role = reducer["roles"][role_id]
        for section in ("population", "missing", "actions", "wait", "route", "risk", "learning"):
            assert red_role[section] == pure_role[section], (role_id, section)
        assert red_role["entries"] == pure_role["entries"]
        assert len(red_role["affect_desire_timeseries"]) == len(pure_role["affect_desire_timeseries"])


def test_reducer_matches_pure_summary_field_by_field():
    rows = _fixture_rows()
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), rows)
        reducer_summary = summary_from_archive(path)
        truth = load_binding_truth(path.parent)
        pure_summary = summarize_traces([normalize_trace(row, truth) for row in rows])
        _compare_roles(reducer_summary, pure_summary)

        consumer = reducer_summary["roles"]["consumer"]
        assert consumer["population"]["with_trace"] == 2
        assert consumer["learning"]["downstream_refined"] == 1
        assert consumer["learning"]["constraint_blocked"] == 1
        assert consumer["learning"]["protected_unchanged"] == 1
        assert consumer["route"]["completed_route_evidence"] == 1
        assert consumer["route"]["route_plans"] == 1
        assert consumer["risk"]["risk_exposure_seconds"] == 1.5
        visitor = reducer_summary["roles"]["visitor"]
        assert visitor["missing"]["no_observation"] == 1
        assert visitor["missing"]["affect_not_evaluated"] == 1


def test_async_warming_builds_in_background_without_blocking_first_call():
    import time

    rows = _fixture_rows()
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), rows)
        # Cold cache: non-blocking probe returns nothing, first build is async.
        assert try_summary(path) is None
        assert ensure_summary_async(path) is False

        # The daemon builder folds the archive; poll until ready.
        deadline = time.monotonic() + 2.0
        summary = None
        while time.monotonic() < deadline:
            summary = try_summary(path)
            if summary is not None:
                break
            time.sleep(0.02)
        assert summary is not None
        assert ensure_summary_async(path) is True
        assert summary["roles"]["consumer"]["population"]["with_trace"] == 2


def test_reducer_cache_warm_hit_keeps_state_and_does_not_rebuild():
    rows = _fixture_rows()
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), rows)
        first = summary_from_archive(path)
        stats_after_first = cache_stats(path)
        assert stats_after_first["cached"] is True
        second = summary_from_archive(path)
        assert second == first
        assert cache_stats(path)["cached"] is True
