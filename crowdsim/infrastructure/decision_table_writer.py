"""Lossless, write-only JSONL decision tables; no database or replay reader."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any


FORMAT = "decision_tables_jsonl"
FORMAT_VERSION = 1
REPLAY_SUPPORTED = True  # Plan/route projection + basic real-SUMO replay verified.
TABLE_FILES = {
    "decisions": "decisions.jsonl",
    "profiles": "decision_profiles.jsonl",
    "plans": "decision_plans.jsonl",
    "routes": "decision_routes.jsonl",
    "person_ids": "decision_person_ids.jsonl",
    "neighbors": "decision_neighbors.jsonl",
}
MANIFEST_FILE = "decision_manifest.json"


def json_default(value):
    # Match the original experiment recorder's JSON representation exactly.
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=json_default).encode("utf-8")


class _InternCache:
    """Bounded exact-byte deduplication. Eviction duplicates data, never loses it.

    Bytes-key equality checks collisions; no digest alone establishes identity.
    Cache budgets include a conservative allowance for each index entry.
    """

    def __init__(self, budget: int):
        self.budget = budget
        self.items: OrderedDict[bytes, int] = OrderedDict()
        self.bytes = 0
        self.hits = 0
        self.evictions = 0

    def get(self, payload: bytes):
        if payload not in self.items:
            return None
        self.items.move_to_end(payload)
        self.hits += 1
        return self.items[payload]

    def add(self, payload: bytes, entry: int):
        cost = len(payload) + 160
        if cost > self.budget:
            return
        while self.items and self.bytes + cost > self.budget:
            old, _ = self.items.popitem(last=False)
            self.bytes -= len(old) + 160
            self.evictions += 1
        self.items[payload] = entry
        self.bytes += cost


class DecisionTableWriter:
    def __init__(self, directory: str | Path, *, flush_every_records: int = 256,
                 flush_interval_seconds: float = 1.0, cache_budgets: dict[str, int] | None = None):
        if flush_every_records < 1 or flush_interval_seconds <= 0:
            raise ValueError("flush thresholds must be positive")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        budgets = {"profiles": 4 << 20, "plans": 4 << 20, "routes": 16 << 20, "neighbors": 32 << 20}
        if cache_budgets is not None:
            if set(cache_budgets) - set(budgets):
                raise ValueError("unknown decision cache table")
            budgets.update(cache_budgets)
        if any(value < 0 for value in budgets.values()):
            raise ValueError("cache budgets must be non-negative")
        self.caches = {name: _InternCache(budget) for name, budget in budgets.items()}
        # Person identifiers are short, and their bijection is kept for the run.
        self.person_ids: dict[str, int] = {}
        self.handles = {}
        self.rows = {name: 0 for name in TABLE_FILES}
        self.bytes = {name: 0 for name in TABLE_FILES}
        self.hashes = {name: hashlib.sha256() for name in TABLE_FILES}
        self.flush_every_records = flush_every_records
        self.flush_interval_seconds = flush_interval_seconds
        self.last_flush = time.monotonic()
        self.flushes = 0
        self.closed = False
        self.failed = False
        self.status = "recording"
        self.error = None
        paths = [self.directory / name for name in (*TABLE_FILES.values(), MANIFEST_FILE)]
        if any(path.exists() for path in paths):
            raise FileExistsError("decision tables already exist; start a new run (resume is not supported)")
        try:
            for table, filename in TABLE_FILES.items():
                self.handles[table] = (self.directory / filename).open("xb", buffering=64 << 10)
            # Exclusively reserve the manifest too; never overwrite an old run.
            with (self.directory / MANIFEST_FILE).open("x", encoding="utf-8") as handle:
                json.dump(self.manifest(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except Exception:
            for handle in self.handles.values():
                try:
                    handle.close()
                except Exception:
                    pass
            self.closed = True
            raise

    def manifest(self) -> dict:
        return {
            "format": FORMAT, "format_version": FORMAT_VERSION, "status": self.status,
            "replay_supported": REPLAY_SUPPORTED, "error": self.error,
            "reference": {"table_field": "$ref", "id_field": "id", "scope": "this run"},
            "decision_record_field": "value", "empty_lists": "inline",
            "neighbor_encoding": "ordered person_ids table identifiers; duplicates preserved",
            "flushes": self.flushes,
            "tables": {name: {"file": filename, "rows": self.rows[name], "bytes": self.bytes[name],
                              "sha256": self.hashes[name].hexdigest()}
                       for name, filename in TABLE_FILES.items()},
            "cache": {name: {"budget_bytes": cache.budget, "indexed_bytes_estimate": cache.bytes,
                             "hits": cache.hits, "evictions": cache.evictions}
                      for name, cache in self.caches.items()},
        }

    def _write(self, table: str, payload: bytes) -> int:
        entry = self.rows[table]
        # The envelope keeps original fields separate from storage metadata.
        line = b'{"id":' + str(entry).encode("ascii") + b',"value":' + payload + b'}\n'
        if self.handles[table].write(line) != len(line):
            raise OSError(f"short write to decision table {table}")
        self.rows[table] += 1
        self.bytes[table] += len(line)
        self.hashes[table].update(line)
        return entry

    def _ref(self, table: str, value: Any) -> dict:
        payload = _encode(value)
        cache = self.caches[table]
        entry = cache.get(payload)
        if entry is None:
            entry = self._write(table, payload)
            cache.add(payload, entry)
        return {"$ref": table, "id": entry}

    def _routes(self, value: dict) -> dict:
        stored = dict(value)
        for field in ("edges", "route_edges", "next_route_edges"):
            route = stored.get(field)
            if isinstance(route, list) and route:
                stored[field] = self._ref("routes", route)
        return stored

    def _plan(self, value):
        return self._ref("plans", self._routes(value)) if isinstance(value, dict) else value

    def _neighbors(self, neighbors: list) -> dict:
        # Unexpected JSON values remain lossless too, without coercing IDs.
        if any(not isinstance(person_id, str) for person_id in neighbors):
            return self._ref("neighbors", {"encoding": "original", "ids": neighbors})
        numbers = []
        for person_id in neighbors:
            number = self.person_ids.get(person_id)
            if number is None:
                number = self._write("person_ids", _encode(person_id))
                self.person_ids[person_id] = number
            numbers.append(number)
        return self._ref("neighbors", {"encoding": "person_ids", "ids": numbers})

    def write_record(self, record: dict) -> None:
        if self.closed or self.failed:
            raise RuntimeError("decision writer is closed or failed")
        # Freeze NOW, not when buffers are flushed. Serialization failure happens
        # before writing dependencies, and unknown fields are not filtered out.
        frozen = json.loads(_encode(record))
        if not isinstance(frozen, dict):
            raise TypeError("decision record must be an object")
        try:
            if "plan" in frozen:
                frozen["plan"] = self._plan(frozen["plan"])
            context = frozen.get("context")
            if isinstance(context, dict):
                profile = context.get("profile")
                if isinstance(profile, dict):
                    context["profile"] = self._ref("profiles", profile)
                state = context.get("state")
                if isinstance(state, dict) and "current_plan" in state:
                    state["current_plan"] = self._plan(state["current_plan"])
                observation = context.get("observation")
                if isinstance(observation, dict):
                    neighbors = observation.get("neighbour_ids")
                    if isinstance(neighbors, list) and neighbors:
                        observation["neighbour_ids"] = self._neighbors(neighbors)
            candidates = frozen.get("candidates")
            if isinstance(candidates, list):
                frozen["candidates"] = [self._routes(value) if isinstance(value, dict) else value for value in candidates]
            self._write("decisions", _encode(frozen))
            if (self.rows["decisions"] % self.flush_every_records == 0
                    or time.monotonic() - self.last_flush >= self.flush_interval_seconds):
                self.flush()
        except Exception as exc:
            self.failed = True
            self.error = f"{type(exc).__name__}: {exc}"[:500]
            raise

    def flush(self) -> None:
        if self.closed:
            return
        # Dependencies precede the main table at explicit checkpoints. Buffered
        # data before a checkpoint is not promised durable after a process crash.
        for table in TABLE_FILES:
            if table != "decisions" and not self.handles[table].closed:
                self.handles[table].flush()
        if not self.handles["decisions"].closed:
            self.handles["decisions"].flush()
        self.flushes += 1
        self.last_flush = time.monotonic()

    def close(self, *, aborted: bool = False, reason: str | None = None) -> None:
        if self.closed:
            return
        first_error = None
        self.status = "aborted" if aborted or self.failed else "complete"
        if reason and not self.failed:
            self.error = str(reason)[:500]
        try:
            self.flush()
        except Exception as exc:
            first_error = exc
            self.failed = True
            self.status = "aborted"
            self.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            for handle in self.handles.values():
                try:
                    if not handle.closed:
                        handle.close()
                except Exception as exc:
                    self.failed = True
                    self.status = "aborted"
                    self.error = f"{type(exc).__name__}: {exc}"[:500]
                    if first_error is None:
                        first_error = exc
            self.closed = all(handle.closed for handle in self.handles.values())
        try:
            (self.directory / MANIFEST_FILE).write_bytes(_encode(self.manifest()) + b"\n")
        except Exception as exc:
            self.status = "aborted"
            self.failed = True
            self.error = f"{type(exc).__name__}: {exc}"[:500]
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error
