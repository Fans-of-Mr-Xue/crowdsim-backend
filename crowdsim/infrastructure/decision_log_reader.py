"""Read-only, bounded-memory plan projection for legacy and split decision logs."""

from array import array
from collections import OrderedDict
from dataclasses import fields
import hashlib
import json
import math
from pathlib import Path

from crowdsim.domain.crowdsim_models import BehaviorPlan
from crowdsim.infrastructure.decision_table_writer import FORMAT, FORMAT_VERSION, MANIFEST_FILE, TABLE_FILES

LEGACY_FORMAT = "decision_inline_jsonl"


class DecisionLogError(ValueError):
    pass


def _json(line, location):
    try:
        return json.loads(line)
    except (ValueError, UnicodeError) as exc:
        raise DecisionLogError(f"invalid JSON at {location}: {exc}") from exc


def _envelope(raw, expected, location):
    if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] != expected or "value" not in raw:
        raise DecisionLogError(f"invalid or non-contiguous table row at {location}")
    return raw["value"]


class DecisionLogReader:
    """Projects only proposed plans and their routes, not full context records.

    Plan offsets cost eight bytes per row; full plan objects are never retained
    for the whole run. The byte-budgeted cache stores raw JSON, not decoded data.
    """

    def __init__(self, directory, *, cache_bytes=4 << 20):
        self.directory = Path(directory)
        self.closed = False
        self._handles = {}
        self._cache = OrderedDict()
        self._cache_size = 0
        if cache_bytes < 0:
            raise ValueError("cache_bytes must be non-negative")
        self.cache_budget = cache_bytes
        self.offsets = {}
        self.records_read = 0
        self.manifest = _json((self.directory / "manifest.json").read_bytes(), "manifest.json")
        if not isinstance(self.manifest, dict):
            raise DecisionLogError("experiment manifest must be an object")
        declared = self.manifest.get("decision_log_format")
        if declared is None:
            if (self.directory / MANIFEST_FILE).exists():
                raise DecisionLogError("split manifest exists but experiment format is missing")
            self.format, self.format_version = LEGACY_FORMAT, 0
        elif (declared == FORMAT and type(self.manifest.get("decision_log_version")) is int
              and self.manifest["decision_log_version"] == FORMAT_VERSION):
            self.format, self.format_version = FORMAT, FORMAT_VERSION
        else:
            raise DecisionLogError(f"unsupported decision format/version: {declared}/{self.manifest.get('decision_log_version')}")
        self.table_manifest = None
        try:
            if self.format == FORMAT:
                self.table_manifest = _json((self.directory / MANIFEST_FILE).read_bytes(), MANIFEST_FILE)
                if not isinstance(self.table_manifest, dict):
                    raise DecisionLogError("decision manifest must be an object")
                if (self.table_manifest.get("format"), self.table_manifest.get("format_version")) != (FORMAT, FORMAT_VERSION):
                    raise DecisionLogError("decision manifests disagree")
                if self.table_manifest.get("status") != "complete":
                    raise DecisionLogError("decision tables are not a completed run")
                if not isinstance(self.table_manifest.get("tables"), dict):
                    raise DecisionLogError("decision manifest has no valid tables mapping")
                # Check presence of the complete format; only replay dependencies
                # are scanned/decoded. Other tables are not claimed validated.
                for filename in TABLE_FILES.values():
                    if not (self.directory / filename).is_file():
                        raise DecisionLogError(f"missing decision table: {filename}")
                for table in ("plans", "routes", "decisions"):
                    self._scan(table, index=table != "decisions")
            elif not (self.directory / TABLE_FILES["decisions"]).is_file():
                raise DecisionLogError("missing decisions.jsonl")
        except Exception:
            self.close()
            raise

    def _scan(self, table, *, index):
        path = self.directory / TABLE_FILES[table]
        metadata = self.table_manifest.get("tables", {}).get(table, {})
        if (not isinstance(metadata, dict) or metadata.get("file") != path.name
                or any(type(metadata.get(name)) is not int or metadata[name] < 0 for name in ("rows", "bytes"))):
            raise DecisionLogError(f"invalid table manifest for {table}")
        offsets = array("Q")
        digest = hashlib.sha256()
        rows = size = 0
        handle = path.open("rb")
        self._handles[table] = handle
        while True:
            position = handle.tell()
            line = handle.readline()
            if not line:
                break
            digest.update(line)
            size += len(line)
            if not line.endswith(b"\n"):
                raise DecisionLogError(f"truncated table {table}:{rows + 1}")
            if index:
                _envelope(_json(line, f"{table}:{rows + 1}"), rows, table)
                offsets.append(position)
            rows += 1
        if (rows, size, digest.hexdigest()) != (metadata.get("rows"), metadata.get("bytes"), metadata.get("sha256")):
            raise DecisionLogError(f"row count, size or checksum mismatch in {table}")
        if index:
            self.offsets[table] = offsets
        handle.seek(0)

    def _lookup(self, reference, table):
        if (not isinstance(reference, dict) or set(reference) != {"$ref", "id"}
                or reference["$ref"] != table or type(reference["id"]) is not int):
            raise DecisionLogError(f"invalid {table} reference: {reference!r}")
        entry = reference["id"]
        if entry < 0 or entry >= len(self.offsets[table]):
            raise DecisionLogError(f"missing {table} reference {entry}")
        key = (table, entry)
        line = self._cache.get(key)
        if line is not None:
            self._cache.move_to_end(key)
        else:
            handle = self._handles[table]
            handle.seek(self.offsets[table][entry])
            line = handle.readline()
            cost = len(line) + 160
            if cost <= self.cache_budget:
                while self._cache and self._cache_size + cost > self.cache_budget:
                    _, old = self._cache.popitem(last=False)
                    self._cache_size -= len(old) + 160
                self._cache[key] = line
                self._cache_size += cost
        return _envelope(_json(line, f"{table}:{entry}"), entry, table)

    def iter_plans(self):
        if self.closed:
            raise RuntimeError("decision reader is closed")
        allowed = {field.name for field in fields(BehaviorPlan)}
        with (self.directory / TABLE_FILES["decisions"]).open("rb") as handle:
            self._handles["iteration"] = handle
            for number, line in enumerate(handle, 1):
                if self.format == LEGACY_FORMAT and not line.strip():
                    continue
                raw = _json(line, f"decisions:{number}")
                if self.format == FORMAT:
                    raw = _envelope(raw, number - 1, f"decisions:{number}")
                if not isinstance(raw, dict) or "plan" not in raw:
                    raise DecisionLogError(f"missing proposed plan at decisions:{number}")
                plan = self._lookup(raw["plan"], "plans") if self.format == FORMAT else raw["plan"]
                if not isinstance(plan, dict):
                    raise DecisionLogError(f"plan is not an object at decisions:{number}")
                plan = dict(plan)
                for name in ("route_edges", "next_route_edges"):
                    route = plan.get(name, [])
                    if self.format == FORMAT and isinstance(route, dict):
                        route = self._lookup(route, "routes")
                    if not isinstance(route, list) or any(not isinstance(edge, str) for edge in route):
                        raise DecisionLogError(f"invalid {name} at decisions:{number}")
                    plan[name] = tuple(route)
                if set(plan) - allowed:
                    raise DecisionLogError(f"unsupported BehaviorPlan fields: {sorted(set(plan) - allowed)}")
                try:
                    result = BehaviorPlan(**plan)
                except TypeError as exc:
                    raise DecisionLogError(f"invalid BehaviorPlan at decisions:{number}: {exc}") from exc
                if (not isinstance(result.person_id, str) or isinstance(result.decided_at, bool)
                        or not isinstance(result.decided_at, (int, float)) or not math.isfinite(result.decided_at)):
                    raise DecisionLogError(f"invalid plan identity/time at decisions:{number}")
                self.records_read += 1
                yield result

    def iter_plan_batches(self):
        previous = None
        batch = []
        for plan in self.iter_plans():
            timestamp = round(plan.decided_at, 6)
            if previous is not None and timestamp < previous:
                raise DecisionLogError("decision times are out of order; streaming replay requires ordered input")
            if batch and timestamp != previous:
                yield previous, batch
                batch = []
            previous = timestamp
            batch.append(plan)
        if batch:
            yield previous, batch

    def close(self):
        for handle in self._handles.values():
            handle.close()
        self._cache.clear()
        self._cache_size = 0
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
