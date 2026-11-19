"""Read-only archive index, person rollup and bounded pagination contracts.

QA stubs only: a temporary JSONL archive is written by the fixture itself; the
index/reader never writes the archive, learner, or control collection. Owned-run
validation on the repository layer mirrors the existing archive back-read.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from crowdsim.infrastructure.decision_trace import VERSION as TRACE_VERSION
from crowdsim.infrastructure.decision_trace_archive import (
    list_archive_persons,
    load_archive_traces,
    page_archive_traces,
)


RUNTIME_ID = "run-" + "a" * 32


def _row(person: str, role: str, snapshot: str, time: float, action: str = "continue") -> dict:
    return {
        "version": TRACE_VERSION,
        "run_id": RUNTIME_ID,
        "person_id": person,
        "role_id": role,
        "snapshot_id": snapshot,
        "time_seconds": time,
        "observation": {"status": "observed"},
        "affect": {"status": "evaluated"},
        "learning": {"attached": False},
        "plan": {"proposed_action": action},
        "participation": [],
    }


def _write_archive(root: Path, rows: list[dict], *, mode: str = "w") -> Path:
    directory = root / RUNTIME_ID
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "decision_traces.jsonl"
    with path.open(mode, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def test_person_index_rollup_counts_and_orders():
    rows = [
        _row("p", "consumer", "r:1", 1.0),
        _row("q", "visitor", "r:1", 1.0),
        _row("p", "consumer", "r:2", 2.0),
        _row("q", "visitor", "r:2", 2.0),
        _row("p", "consumer", "r:3", 3.0),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        _write_archive(Path(tmp), rows)
        persons, total, missing = list_archive_persons(RUNTIME_ID, root=tmp)
        assert missing == []
        assert total == 2
        by = {item["person_id"]: item for item in persons}
        assert [item["person_id"] for item in persons] == ["p", "q"]
        assert by["p"] == {"person_id": "p", "role_id": "consumer", "trace_count": 3,
                           "first_time_seconds": 1.0, "last_time_seconds": 3.0}
        assert by["q"]["trace_count"] == 2
        assert by["q"]["first_time_seconds"] == 1.0
        assert by["q"]["last_time_seconds"] == 2.0


def test_pagination_total_cursor_and_full_travserse():
    rows = [_row("p", "consumer", f"r:{i}", float(i)) for i in range(1, 6)]
    with tempfile.TemporaryDirectory() as tmp:
        _write_archive(Path(tmp), rows)
        first, total, next_cursor, missing = page_archive_traces(
            RUNTIME_ID, root=tmp, page_size=2, cursor=0)
        assert missing == []
        assert total == 5
        assert len(first) == 2
        assert next_cursor == 2

        second, total, next_cursor, _ = page_archive_traces(
            RUNTIME_ID, root=tmp, page_size=2, cursor=next_cursor)
        assert total == 5
        assert len(second) == 2
        assert next_cursor == 4

        third, total, next_cursor, _ = page_archive_traces(
            RUNTIME_ID, root=tmp, page_size=2, cursor=next_cursor)
        assert len(third) == 1
        assert next_cursor is None

        # Every boundary is reached across pages; nothing is dropped.
        all_times = [row["time_seconds"] for row in first + second + third]
        assert sorted(all_times) == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_person_snapshot_time_filters_match_index():
    rows = [
        _row("p", "consumer", "r:1", 1.0),
        _row("q", "visitor", "r:1", 1.0),
        _row("p", "consumer", "r:2", 2.0),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        _write_archive(Path(tmp), rows)
        only_p, _ = load_archive_traces(RUNTIME_ID, root=tmp, person_id="p")
        assert [row["person_id"] for row in only_p] == ["p", "p"]
        snap, _ = load_archive_traces(RUNTIME_ID, root=tmp, snapshot_id="r:2")
        assert len(snap) == 1
        time, _ = load_archive_traces(RUNTIME_ID, root=tmp, time_seconds=1.0)
        assert len(time) == 2


def test_append_only_growth_is_indexed_incrementally():
    first_batch = [_row("p", "consumer", f"r:{i}", float(i)) for i in range(1, 4)]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_archive(root, first_batch)
        _, total, _ = list_archive_persons(RUNTIME_ID, root=tmp)
        assert total == 1
        # Append two more trace lines; the index resumes from the tail offset.
        with path.open("a", encoding="utf-8") as handle:
            for row in [_row("p", "consumer", "r:4", 4.0), _row("q", "visitor", "r:4", 4.0)]:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        persons, total, _ = list_archive_persons(RUNTIME_ID, root=tmp)
        assert total == 2
        by = {item["person_id"]: item for item in persons}
        assert by["p"]["trace_count"] == 4
        assert by["q"]["trace_count"] == 1


def test_truncated_file_rebuilds_index():
    rows = [_row("p", "consumer", f"r:{i}", float(i)) for i in range(1, 4)]
    with tempfile.TemporaryDirectory() as tmp:
        _write_archive(Path(tmp), rows)
        _, total, _ = list_archive_persons(RUNTIME_ID, root=tmp)
        assert total == 1
        # Rewrite with fewer rows (truncate/rewrite) -> full rebuild, not stale.
        _write_archive(Path(tmp), [_row("q", "visitor", "r:1", 1.0)], mode="w")
        persons, total, _ = list_archive_persons(RUNTIME_ID, root=tmp)
        assert total == 1
        assert persons[0]["person_id"] == "q"


def test_missing_archive_returns_explicit_reasons():
    with tempfile.TemporaryDirectory() as tmp:
        _, missing = load_archive_traces("run-" + "b" * 32, root=tmp)
        assert missing == ["runtime_archive_not_found"]
        _, missing = load_archive_traces("not-a-runtime-id", root=tmp)
        assert missing == ["no_runtime_run_id"]
        (Path(tmp) / RUNTIME_ID).mkdir(parents=True)
        _, missing = load_archive_traces(RUNTIME_ID, root=tmp)
        assert missing == ["trace_archive_not_found"]
        _write_archive(Path(tmp), [_row("p", "consumer", "r:1", 1.0)])
        _, missing = load_archive_traces(RUNTIME_ID, root=tmp, person_id="nobody")
        assert missing == ["no_matching_boundary"]


def test_repository_person_index_validates_owned_runtime():
    from crowdsim.experiments.repository import CrowdSimRepository

    repo = CrowdSimRepository(None)
    current = "run-" + "a" * 32
    previous = "run-" + "b" * 32
    run = repo.create_run("u", "w", "exp-x", "linucb_v1", 0, {})
    repo.update_run("u", "w", run["run_id"],
                    {"runtime_run_id": current, "previous_runtime_run_ids": [previous]})

    result = repo.read_archive_person_index("u", "w", run["run_id"], runtime_run_id=previous)
    assert result["selected_runtime_run_id"] == previous
    assert result["runtime_run_id"] == current
    assert [previous] == result["previous_runtime_run_ids"]
    assert result["mixed_run"] is False
    assert result["persons"] == []
    assert result["total"] == 0

    with __import__("pytest").raises(LookupError) as ctx:
        repo.read_archive_person_index("u", "w", run["run_id"],
                                       runtime_run_id="run-" + "c" * 32)
    raised = ctx.value if hasattr(ctx, "value") else ctx.exception
    assert raised.args[0] == "RUNTIME_RUN_ID_NOT_OWNED"


def test_paginated_response_excludes_role_summary():
    rows = [_row("p", "consumer", f"r:{i}", float(i)) for i in range(1, 4)]
    with tempfile.TemporaryDirectory() as tmp:
        _write_archive(Path(tmp), rows)
        traces, total, next_cursor, missing = page_archive_traces(
            RUNTIME_ID, root=tmp, page_size=2)
        assert total == 3
        assert next_cursor == 2
        assert len(traces) == 2
        assert missing == []
