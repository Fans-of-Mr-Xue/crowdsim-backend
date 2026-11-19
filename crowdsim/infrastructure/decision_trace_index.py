"""Read-only incremental sidecar index for archived ``decision_traces.jsonl``.

The runtime appends boundary traces to an append-only JSONL. On the control
back-read side a full ``json.loads`` of a multi-hundred-MB file per request is
unusable for the single-person and role history pages. This module builds a
lightweight in-process index over that same archive:

- records the byte offset of every valid trace line plus the top-level boundary
  fields only (``person_id``, ``role_id``, ``snapshot_id``, ``time_seconds``);
- caches the index per archive path, reused while ``(size, mtime_ns)`` is
  unchanged; an append-only growth continues scanning from the recorded
  offset, while a truncated/rewritten file rebuilds from scratch;
- full trace bodies are read back on demand for the matching offset slice.

The index never writes the archive, the learner, or any control collection; it
is a derived, read-only view of the persisted source.
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any

from crowdsim.infrastructure.decision_trace import VERSION

_LOCK = threading.RLock()
_CACHE: dict[str, dict[str, Any]] = {}


def _finite(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    return value


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _stat_identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _scan(path: Path, state: dict[str, Any]) -> None:
    """Scan new lines from the recorded offset; append index entries."""
    with path.open("rb") as handle:
        if state["next_offset"] > 0:
            handle.seek(state["next_offset"])
        else:
            handle.seek(0)
        while True:
            offset = handle.tell()
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
            if not isinstance(row, dict) or row.get("version") != VERSION:
                continue
            state["entries"].append({
                "line": len(state["entries"]),
                "offset": offset,
                "person_id": row.get("person_id"),
                "role_id": row.get("role_id"),
                "snapshot_id": row.get("snapshot_id"),
                "time_seconds": _finite(row.get("time_seconds")),
            })


def indexed_entries(path: str | Path) -> list[dict[str, Any]]:
    """Return the cached index entries for the archive, rebuilding if needed."""
    resolved = _resolve(path)
    key = str(resolved)
    with _LOCK:
        identity = _stat_identity(resolved)
        cached = _CACHE.get(key)
        if cached is not None:
            if cached["identity"] == identity:
                return cached["entries"]
            # Append-only continuation: file grew, resume from the recorded
            # tail offset. A smaller file (truncate/rewrite) falls through to
            # a full rebuild.
            if identity[0] >= cached["next_offset"] and cached["entries"]:
                cached["identity"] = identity
                state = cached
            else:
                state = {"identity": identity, "entries": [], "next_offset": 0}
                _CACHE[key] = state
        else:
            state = {"identity": identity, "entries": [], "next_offset": 0}
            _CACHE[key] = state
        _scan(resolved, state)
        return state["entries"]


def match_entries(entries: list[dict[str, Any]], *, person_id: str | None = None,
                  snapshot_id: str | None = None,
                  time_seconds: float | None = None) -> list[dict[str, Any]]:
    """Filter index entries by boundary fields (top-level equality)."""
    matched: list[dict[str, Any]] = []
    for entry in entries:
        if person_id is not None and entry["person_id"] != person_id:
            continue
        if snapshot_id is not None and entry["snapshot_id"] != snapshot_id:
            continue
        if time_seconds is not None and entry["time_seconds"] != time_seconds:
            continue
        matched.append(entry)
    return matched


def read_rows(path: str | Path, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read back full trace bodies for the given index offset entries."""
    resolved = _resolve(path)
    rows: list[dict[str, Any]] = []
    with resolved.open("rb") as handle:
        for entry in entries:
            handle.seek(entry["offset"])
            text = handle.readline().decode("utf-8", errors="replace").strip()
            if not text:
                continue
            row = json.loads(text)
            if isinstance(row, dict) and row.get("version") == VERSION:
                rows.append(row)
    return rows


def person_view(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lightweight per-person rollup for the history list page."""
    grouped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        person_id = entry["person_id"]
        if person_id is None:
            continue
        item = grouped.setdefault(person_id, {
            "person_id": person_id,
            "role_id": entry["role_id"],
            "trace_count": 0,
            "first_time_seconds": entry["time_seconds"],
            "last_time_seconds": entry["time_seconds"],
        })
        item["trace_count"] += 1
        item["last_time_seconds"] = entry["time_seconds"]
        if item["role_id"] is None:
            item["role_id"] = entry["role_id"]
    return sorted(grouped.values(), key=lambda item: str(item["person_id"]))
