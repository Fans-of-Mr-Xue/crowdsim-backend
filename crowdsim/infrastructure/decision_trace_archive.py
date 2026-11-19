"""Read-only archive back-read for decision traces persisted by the runtime.

The runtime appends each trace to ``<runtime_root>/<runtime_run_id>/decision_traces.jsonl``
(append-only, version ``decision-trace-v1``). This module re-reads that same
source after a reconnect or on the control side, filtered by
run/person/snapshot/time. It never writes, replays into the learner, invokes
decision or SUMO machinery, or fabricates a row when the archive is absent.

Missing archive conditions are reported explicitly as ``missing`` reasons;
an empty trace list means the archive simply has no recorded boundary yet.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from crowdsim.infrastructure.decision_trace_index import (
    indexed_entries,
    match_entries,
    person_view,
    read_rows,
)
from crowdsim.infrastructure.archive_learning_gate import (
    load_binding_truth,
    normalize_trace,
)
from crowdsim.infrastructure.decision_trace_reducer import (
    ensure_summary_async,
    try_summary,
)
from crowdsim.infrastructure.role_summary import summarize_traces

RUNTIME_ID_RE = re.compile(r"run-[0-9a-f]{32}")
TRACE_FILE = "decision_traces.jsonl"


def _runtime_root(root: str | Path | None) -> Path:
    return Path(root).expanduser().resolve() if root else Path(__file__).resolve().parents[2] / "runs"


def resolve_runtime_dir(runtime_run_id: str | None, *, root: str | Path | None = None) -> Path | None:
    """Return the verified runtime directory, or ``None`` when it is absent/unsafe."""
    if not isinstance(runtime_run_id, str) or not RUNTIME_ID_RE.fullmatch(runtime_run_id):
        return None
    base = _runtime_root(root).resolve()
    candidate = (base / runtime_run_id).resolve()
    if candidate.parent != base or candidate.is_symlink():
        return None
    if not candidate.is_dir():
        return None
    return candidate


def trace_archive_path(runtime_run_id: str | None, *, root: str | Path | None = None) -> Path | None:
    directory = resolve_runtime_dir(runtime_run_id, root=root)
    return None if directory is None else directory / TRACE_FILE


def _archive_path(runtime_run_id: str | None, root: str | Path | None) -> tuple[Path | None, list[str]]:
    missing: list[str] = []
    if not isinstance(runtime_run_id, str) or not RUNTIME_ID_RE.fullmatch(runtime_run_id):
        return None, ["no_runtime_run_id"]
    directory = resolve_runtime_dir(runtime_run_id, root=root)
    if directory is None:
        return None, ["runtime_archive_not_found"]
    path = directory / TRACE_FILE
    if not path.is_file():
        return None, ["trace_archive_not_found"]
    return path, missing


def _filter_entries(path: Path, *, person_id: str | None, snapshot_id: str | None,
                    time_seconds: float | None) -> tuple[list[dict[str, Any]], list[str]]:
    missing: list[str] = []
    entries = indexed_entries(path)
    if not entries:
        missing.append("no_trace_records")
        return [], missing
    matched = match_entries(entries, person_id=person_id, snapshot_id=snapshot_id,
                            time_seconds=time_seconds)
    if not matched:
        missing.append("no_matching_boundary")
    return matched, missing


def _project_rows(rows: list[dict[str, Any]], directory: Path) -> list[dict[str, Any]]:
    """Re-project learning attribution with same-archive binding proof (read-only)."""
    truth = load_binding_truth(directory)
    return [normalize_trace(row, truth) for row in rows]


def load_archive_traces(
    runtime_run_id: str | None,
    *,
    root: str | Path | None = None,
    person_id: str | None = None,
    snapshot_id: str | None = None,
    time_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read archived traces with boundary filters; return (traces, missing reasons)."""
    path, missing = _archive_path(runtime_run_id, root)
    if path is None:
        return [], missing
    matched, filter_missing = _filter_entries(
        path, person_id=person_id, snapshot_id=snapshot_id, time_seconds=time_seconds)
    rows = read_rows(path, matched)
    return _project_rows(rows, path.parent), [*missing, *filter_missing]


def list_archive_persons(runtime_run_id: str | None, *, root: str | Path | None = None,
                         ) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Lightweight per-person rollup for the history list page (no body readback)."""
    path, missing = _archive_path(runtime_run_id, root)
    if path is None:
        return [], 0, missing
    entries = indexed_entries(path)
    if not entries:
        return [], 0, [*missing, "no_trace_records"]
    persons = person_view(entries)
    return persons, len(persons), missing


def page_archive_traces(
    runtime_run_id: str | None,
    *,
    root: str | Path | None = None,
    person_id: str | None = None,
    snapshot_id: str | None = None,
    time_seconds: float | None = None,
    cursor: int | None = None,
    page_size: int | None = None,
) -> tuple[list[dict[str, Any]], int, int | None, list[str]]:
    """Bounded page of archived traces.

    Returns ``(traces, total, next_cursor, missing)``. ``total`` counts all
    matching boundaries; ``next_cursor`` is the offset for the next page or
    ``None`` on the last page. The full archive is not truncated: later pages
    repeat the same filters with the returned cursor.
    """
    path, missing = _archive_path(runtime_run_id, root)
    if path is None:
        return [], 0, None, missing
    matched, filter_missing = _filter_entries(
        path, person_id=person_id, snapshot_id=snapshot_id, time_seconds=time_seconds)
    total = len(matched)
    size = max(1, min(50, int(page_size or 50)))
    start = max(0, int(cursor or 0))
    if start > total:
        start = total
    stop = min(total, start + size)
    page_entries = matched[start:stop]
    next_cursor = stop if stop < total else None
    rows = read_rows(path, page_entries)
    return _project_rows(rows, path.parent), total, next_cursor, [*missing, *filter_missing]


def archive_summary(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate archived traces by role; the same read-only summary used live."""
    return summarize_traces(traces)


def archive_role_summary(runtime_run_id: str | None, *, root: str | Path | None = None,
                         ) -> tuple[dict[str, Any] | None, bool, list[str]]:
    """Role summary from the streaming reducer cache, non-blocking on first build.

    Returns ``(summary, warming, missing)``. When the reducer cache matches the
    current archive identity the folded summary is returned with ``warming``
    ``False``. Otherwise a background build is triggered and the response
    reports ``warming`` ``True`` with ``summary`` ``None``; the caller polls the
    same endpoint until the build finishes. The archive is never written.
    """
    path, missing = _archive_path(runtime_run_id, root)
    if path is None:
        return None, False, missing
    summary = try_summary(path)
    if summary is not None:
        return summary, False, missing
    ensure_summary_async(path)
    return None, True, [*missing, "summary_warming"]
