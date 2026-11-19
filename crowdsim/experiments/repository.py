"""File-backed repository for standalone CrowdSim control experiments."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import uuid
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(value):
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class CrowdSimRepository:
    """Persist experiments under ``runs/control_experiments`` without MongoDB."""

    COLLECTIONS = (
        "experiments", "runs", "observations", "decisions", "acks",
        "evaluations", "metrics", "llm", "reports", "leases", "presentation_epochs",
    )
    APPEND_ONLY = {"observations", "decisions", "acks", "evaluations", "metrics", "llm"}

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._root = Path(data_dir).expanduser().resolve() if data_dir else None
        self._lock = threading.RLock()
        self._memory: dict[str, list[dict[str, Any]]] = {name: [] for name in self.COLLECTIONS}
        if self._root is not None:
            self._root.mkdir(parents=True, exist_ok=True)
            self._load()
        from .final_frame_archive import FinalFrameArchive
        self.final_frames = FinalFrameArchive(self._root / 'final_frames' if self._root is not None else None)

    def allocate_presentation_generation(self):
        """Allocate a durable epoch even if separate instances start together."""
        from contextlib import nullcontext
        import os
        with self._lock:
            lock = (self._root / 'presentation_epochs.lock').open('a+b') if self.persistent else nullcontext(None)
            with lock as handle:
                if handle is not None:
                    if os.name == 'nt':
                        import msvcrt
                        handle.seek(0, 2)
                        if handle.tell() == 0:
                            handle.write(b'0'); handle.flush()
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                old = self._memory['presentation_epochs']
                try:
                    current = old
                    if handle is not None and self._path('presentation_epochs').exists():
                        current = json.loads(self._path('presentation_epochs').read_text(encoding='utf-8'))
                    if not isinstance(current, list) or any(type(r.get('generation')) is not int or r['generation'] < 1 for r in current):
                        raise ValueError('invalid persisted presentation generation')
                    generation = max((r['generation'] for r in current), default=0) + 1
                    self._memory['presentation_epochs'] = [{'generation': generation}]
                    self._flush('presentation_epochs')
                    return generation
                except Exception:
                    self._memory['presentation_epochs'] = old
                    raise
                finally:
                    if handle is not None:
                        if os.name == 'nt':
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def save_final_frame(self, experiment, run, view):
        with self._lock:
            return self.final_frames.save(experiment, run, view)

    def read_final_frame(self, experiment, run):
        with self._lock:
            return self.final_frames.read(experiment, run, expected_sha256=run.get('finalFrameSha256'))

    @property
    def persistent(self) -> bool:
        return self._root is not None

    def _path(self, key: str) -> Path:
        if self._root is None:
            raise RuntimeError("repository is running in memory")
        return self._root / (f"{key}.jsonl" if key in self.APPEND_ONLY else f"{key}.json")

    def _load(self) -> None:
        for key in self.COLLECTIONS:
            path = self._path(key)
            if not path.is_file():
                continue
            try:
                if key in self.APPEND_ONLY:
                    values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                else:
                    values = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(values, list):
                    self._memory[key] = [item for item in values if isinstance(item, dict)]
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"cannot load CrowdSim experiment data from {path}: {exc}") from exc

    def _flush(self, key: str) -> None:
        if not self.persistent:
            return
        path = self._path(key)
        if key in self.APPEND_ONLY:
            text = "".join(json.dumps(_serialize(item), ensure_ascii=False, separators=(",", ":")) + "\n" for item in self._memory[key])
        else:
            text = json.dumps(_serialize(self._memory[key]), ensure_ascii=False, indent=2)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        try:
            temporary.replace(path)
        except PermissionError:
            # Some Windows file-system providers reject atomic replacement even
            # though both files are writable. Preserve persistence in that case;
            # normal local disks still use the atomic path above.
            path.write_text(text, encoding="utf-8")
            temporary.unlink(missing_ok=True)

    def _insert(self, key: str, document: dict[str, Any]) -> dict[str, Any]:
        if key not in self._memory:
            raise ValueError(f"unknown record kind: {key}")
        doc = deepcopy(document)
        with self._lock:
            self._memory[key].append(doc)
            if self.persistent and key in self.APPEND_ONLY:
                with self._path(key).open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(_serialize(doc), ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                self._flush(key)
        return _serialize(deepcopy(doc))

    def _find_one(self, key: str, query: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            doc = next((item for item in self._memory[key] if all(item.get(name) == value for name, value in query.items())), None)
        return _serialize(deepcopy(doc)) if doc else None

    def _update(self, key: str, query: dict[str, Any], values: dict[str, Any]) -> dict[str, Any] | None:
        values = {**deepcopy(values), "updated_at": _now()}
        with self._lock:
            for item in self._memory[key]:
                if all(item.get(name) == value for name, value in query.items()):
                    item.update(values)
                    self._flush(key)
                    break
        return self._find_one(key, query)

    def create_experiment(self, user_id: str, workspace_id: str, config: dict[str, Any]) -> dict[str, Any]:
        # A browser may lose the create response. Retrying the same confirmed
        # draft must recover its experiment, including after a service restart.
        with self._lock:
            request_id = config.get("clientRequestId")
            if request_id:
                existing = self._find_one("experiments", {
                    "user_id": str(user_id), "workspace_id": str(workspace_id), "clientRequestId": request_id,
                })
                if existing:
                    if any(existing.get(key) != value for key, value in config.items()):
                        raise ValueError("CLIENT_REQUEST_CONFLICT")
                    return existing
            now = _now()
            return self._insert("experiments", {
                **deepcopy(config), "experiment_id": f"exp_{uuid.uuid4().hex}", "user_id": str(user_id),
                "workspace_id": str(workspace_id), "status": "draft", "created_at": now, "updated_at": now,
            })

    def get_experiment(self, user_id: str, workspace_id: str, experiment_id: str):
        return self._find_one("experiments", {"user_id": str(user_id), "workspace_id": str(workspace_id), "experiment_id": str(experiment_id)})

    def list_experiments(self, user_id: str, workspace_id: str, limit: int = 50, offset: int = 0, *, summary: bool = False):
        query = {"user_id": str(user_id), "workspace_id": str(workspace_id)}
        with self._lock:
            docs = [deepcopy(item) for item in self._memory["experiments"] if all(item.get(k) == v for k, v in query.items())]
            docs = list(reversed(docs))[max(0, offset):max(0, offset) + max(1, int(limit))]
            if summary:
                run_groups = {doc["experiment_id"]: [] for doc in docs}
                for run in self._memory["runs"]:
                    if all(run.get(k) == v for k, v in query.items()) and run.get("experiment_id") in run_groups:
                        run_groups[run["experiment_id"]].append({key: deepcopy(run.get(key)) for key in ("run_id", "status", "error")})
                docs = [{**{key: doc.get(key) for key in ("experiment_id", "clientRequestId", "name", "status", "created_at", "updated_at", "duringObservation", "executionVersion", "methods")},
                         "scenario": {"presetId": (doc.get("scenario") or {}).get("presetId")},
                         "runs": run_groups[doc["experiment_id"]]} for doc in docs]
        return [_serialize(doc) for doc in docs]

    def update_experiment(self, user_id, workspace_id, experiment_id, values):
        return self._update("experiments", {"user_id": str(user_id), "workspace_id": str(workspace_id), "experiment_id": str(experiment_id)}, values)

    def create_run(self, user_id, workspace_id, experiment_id, method_id, seed, config, *, formal=True):
        now = _now()
        return self._insert("runs", {
            "run_id": f"run_{uuid.uuid4().hex}", "user_id": str(user_id), "workspace_id": str(workspace_id),
            "experiment_id": str(experiment_id), "method_id": str(method_id), "seed": int(seed),
            "status": "draft", "config": deepcopy(config), "controller_state": None,
            "formal": bool(formal), "manualIntervention": False, "created_at": now, "updated_at": now,
            "started_at": None, "completed_at": None, "error": None,
        })

    def get_run(self, user_id, workspace_id, run_id):
        return self._find_one("runs", {"user_id": str(user_id), "workspace_id": str(workspace_id), "run_id": str(run_id)})

    def list_runs(self, user_id, workspace_id, experiment_id=None):
        query = {"user_id": str(user_id), "workspace_id": str(workspace_id)}
        if experiment_id is not None:
            query["experiment_id"] = str(experiment_id)
        with self._lock:
            docs = [deepcopy(item) for item in self._memory["runs"] if all(item.get(k) == v for k, v in query.items())]
        return [_serialize(item) for item in docs]

    def update_run(self, user_id, workspace_id, run_id, values):
        return self._update("runs", {"user_id": str(user_id), "workspace_id": str(workspace_id), "run_id": str(run_id)}, values)

    def validate_observation_reset(self, user_id, workspace_id, experiment_id, expected_run_ids):
        from .during_observation_reset import runtime_paths, validate_reset
        with self._lock:
            experiment = self.get_experiment(user_id, workspace_id, experiment_id)
            runs = self.list_runs(user_id, workspace_id, experiment_id)
            validate_reset(experiment, runs, expected_run_ids)
            ids = {run["run_id"] for run in runs}
            owns = lambda row: row.get("user_id") == str(user_id) and row.get("workspace_id") == str(workspace_id)
            other_runs = [row for row in self._memory["runs"] if not (owns(row) and row["run_id"] in ids)]
            if any((row.get("duringReplay") or {}).get("sourceExperimentId") == experiment_id
                   for row in self._memory["experiments"] if row["experiment_id"] != experiment_id):
                from .contracts import ContractError
                raise ContractError("此实验仍有关联策略重跑，未清除来源结果。")
            runtime_paths(runs, other_runs)
            return experiment, runs

    def reset_observation(self, user_id, workspace_id, experiment_id, expected_run_ids):
        from .during_observation_reset import remove_runtime_files
        with self._lock:
            experiment, runs = self.validate_observation_reset(user_id, workspace_id, experiment_id, expected_run_ids)
            if not runs and experiment["status"] == "draft":
                return self.get_experiment(user_id, workspace_id, experiment_id)
            ids = {run["run_id"] for run in runs}
            owns = lambda row: row.get("user_id") == str(user_id) and row.get("workspace_id") == str(workspace_id)
            remove_runtime_files(runs, [])
            # Keep the run IDs until every result collection is flushed. If a
            # write fails, another attempt can finish deletion without backups.
            for kind in (*self.APPEND_ONLY, "reports", "runs"):
                def deleted(row):
                    return owns(row) and (row.get("experiment_id") == experiment_id if kind == "reports" else row.get("run_id") in ids)
                previous = self._memory[kind]
                self._memory[kind] = [row for row in previous if not deleted(row)]
                try:
                    self._flush(kind)
                except Exception:
                    self._memory[kind] = previous
                    raise
            version = self.get_experiment(user_id, workspace_id, experiment_id).get("executionVersion", 0)
            document = next(row for row in self._memory["experiments"] if owns(row) and row["experiment_id"] == experiment_id)
            previous = deepcopy(document)
            try:
                self.update_experiment(user_id, workspace_id, experiment_id, {"status": "draft", "stop_requested": False, "executionVersion": version + 1})
            except Exception:
                document.clear(); document.update(previous)
                raise
            return self.get_experiment(user_id, workspace_id, experiment_id)

    def restart_observation_run(self, user_id, workspace_id, run_id):
        """Replace one round's evidence only after its old worker has stopped.

        Keep the old raw runtime directory and its ID until initialization closes
        that runtime. Other rounds, their order, seeds and evidence stay intact.
        A pending flag survives partial writes so cleanup can safely be retried.
        """
        from .contracts import ContractError
        with self._lock:
            run = self.get_run(user_id, workspace_id, run_id)
            experiment = self.get_experiment(user_id, workspace_id, run["experiment_id"]) if run else None
            if not experiment or not experiment.get("duringObservation") or not run.get("restartPending"):
                raise ContractError("没有待重新运行的事中本轮实验。")
            owns = lambda row: row.get("user_id") == str(user_id) and row.get("workspace_id") == str(workspace_id)
            for kind in (*self.APPEND_ONLY, "reports"):
                previous = self._memory[kind]
                self._memory[kind] = [row for row in previous if not (owns(row) and (
                    row.get("experiment_id") == run["experiment_id"] if kind == "reports" else row.get("run_id") == run_id))]
                try:
                    self._flush(kind)
                except Exception:
                    self._memory[kind] = previous
                    raise
            # Rebuild the row so no old summary/completion/model evidence leaks
            # into the new attempt. Keeping the row position preserves round N.
            keep = {key: deepcopy(run[key]) for key in (
                "run_id", "user_id", "workspace_id", "experiment_id", "method_id", "seed", "config",
                "formal", "created_at", "runtime_run_id", "restartCount", "provenance") if key in run}
            history = list(run.get("previous_runtime_run_ids") or [])
            if run.get("runtime_run_id") and run["runtime_run_id"] not in history:
                history.append(run["runtime_run_id"])
            keep["previous_runtime_run_ids"] = history
            keep.update(status="draft", controller_state=None, manualIntervention=False,
                        started_at=None, completed_at=None, error=None, restartPending=False, updated_at=_now())
            index = next(i for i, row in enumerate(self._memory["runs"]) if owns(row) and row["run_id"] == run_id)
            previous = self._memory["runs"][index]
            self._memory["runs"][index] = keep
            try:
                self._flush("runs")
            except Exception:
                self._memory["runs"][index] = previous
                raise
            return self.get_run(user_id, workspace_id, run_id)

    def record(self, kind: str, user_id: str, workspace_id: str, run_id: str, payload: dict[str, Any]):
        if kind not in self.APPEND_ONLY:
            raise ValueError(f"unknown record kind: {kind}")
        identity = f"{kind[:-1]}_id" if kind.endswith("s") else f"{kind}_id"
        return self._insert(kind, {
            identity: f"{kind[:4]}_{uuid.uuid4().hex}", "user_id": str(user_id),
            "workspace_id": str(workspace_id), "run_id": str(run_id), "payload": deepcopy(payload), "created_at": _now(),
        })

    def list_records(self, kind: str, user_id: str, workspace_id: str, run_id: str, limit: int = 1000):
        if kind not in self.APPEND_ONLY:
            raise ValueError(f"unknown record kind: {kind}")
        query = {"user_id": str(user_id), "workspace_id": str(workspace_id), "run_id": str(run_id)}
        with self._lock:
            docs = [deepcopy(item) for item in self._memory[kind] if all(item.get(k) == v for k, v in query.items())]
        return [_serialize(item) for item in docs[:max(1, int(limit))]]

    def _resolve_owned_runtime(self, user_id: str, workspace_id: str, run_id: str,
                               runtime_run_id: str | None):
        run = self.get_run(user_id, workspace_id, run_id)
        if not run:
            raise LookupError("RUN_NOT_FOUND")
        current_runtime_run_id = run.get("runtime_run_id")
        history_runtime_run_ids = list(run.get("previous_runtime_run_ids") or [])
        allowed_runtime_run_ids = [item for item in
                                   (current_runtime_run_id, *history_runtime_run_ids) if item]
        selected_runtime_run_id = runtime_run_id or current_runtime_run_id
        if (selected_runtime_run_id is not None
                and selected_runtime_run_id not in allowed_runtime_run_ids):
            raise LookupError("RUNTIME_RUN_ID_NOT_OWNED")
        return run, current_runtime_run_id, history_runtime_run_ids, selected_runtime_run_id

    def read_decision_trace_archive(self, user_id: str, workspace_id: str, run_id: str,
                                     *, person_id: str | None = None,
                                     snapshot_id: str | None = None,
                                     time_seconds: float | None = None,
                                     runtime_run_id: str | None = None,
                                     cursor: int | None = None,
                                     page_size: int | None = None):
        """Read-only back-read of the runtime ``decision_traces.jsonl`` for a run.

        Traces live in the runtime archive, not the control envelope collections,
        so this never inserts or appends. A run without a runtime id, a missing
        archive, or an empty file returns explicit ``missing`` reasons rather
        than fabricated rows.

        ``runtime_run_id`` selects one owned runtime archive: the current one or
        an earlier one recorded in ``previous_runtime_run_ids``. Archives are
        never merged across runs; a runtime id that does not belong to this run
        is rejected rather than read.

        When ``cursor``/``page_size`` is supplied the response is a bounded page
        with ``total`` and ``next_cursor``; the full archive is not truncated.
        The paginated path does not attach ``role_summary`` (that still scans
        the whole archive) and must be fetched from the role-summary endpoint.
        """
        from crowdsim.infrastructure.decision_trace_archive import (
            load_archive_traces, archive_summary, page_archive_traces)

        run, current_runtime_run_id, history_runtime_run_ids, selected_runtime_run_id = \
            self._resolve_owned_runtime(user_id, workspace_id, run_id, runtime_run_id)
        base = {
            "run_id": run["run_id"],
            "runtime_run_id": current_runtime_run_id,
            "selected_runtime_run_id": selected_runtime_run_id,
            "previous_runtime_run_ids": history_runtime_run_ids,
            "mixed_run": False,
            "trace_file": "decision_traces.jsonl",
        }
        if cursor is not None or page_size is not None:
            traces, total, next_cursor, missing = page_archive_traces(
                selected_runtime_run_id, person_id=person_id, snapshot_id=snapshot_id,
                time_seconds=time_seconds, cursor=cursor, page_size=page_size)
            return {
                **base,
                "missing": missing,
                "traces": [_serialize(row) for row in traces],
                "total": total,
                "next_cursor": next_cursor,
                "role_summary": None,
            }
        traces, missing = load_archive_traces(
            selected_runtime_run_id, person_id=person_id, snapshot_id=snapshot_id, time_seconds=time_seconds)
        return {
            **base,
            "missing": missing,
            "traces": [_serialize(row) for row in traces],
            "role_summary": archive_summary(traces) if time_seconds is None and snapshot_id is None else None,
        }

    def read_archive_person_index(self, user_id: str, workspace_id: str, run_id: str,
                                  *, runtime_run_id: str | None = None):
        """Lightweight per-person rollup for the history list page.

        Reads only the index boundary fields; trace bodies are not loaded.
        Owned-run validation mirrors :meth:`read_decision_trace_archive`.
        """
        from crowdsim.infrastructure.decision_trace_archive import list_archive_persons

        run, current_runtime_run_id, history_runtime_run_ids, selected_runtime_run_id = \
            self._resolve_owned_runtime(user_id, workspace_id, run_id, runtime_run_id)
        persons, total, missing = list_archive_persons(selected_runtime_run_id)
        return {
            "run_id": run["run_id"],
            "runtime_run_id": current_runtime_run_id,
            "selected_runtime_run_id": selected_runtime_run_id,
            "previous_runtime_run_ids": history_runtime_run_ids,
            "mixed_run": False,
            "trace_file": "decision_traces.jsonl",
            "missing": missing,
            "total": total,
            "persons": persons,
        }

    def read_archive_role_summary(self, user_id: str, workspace_id: str, run_id: str,
                                  *, runtime_run_id: str | None = None):
        """Role summary from the streaming reducer cache; no full archive decode on warm hit.

        Owned-run validation mirrors :meth:`read_decision_trace_archive`. The
        reducer folds deduplicated accumulators once per archive identity; the
        response carries no full trace list.
        """
        from crowdsim.infrastructure.decision_trace_archive import archive_role_summary

        run, current_runtime_run_id, history_runtime_run_ids, selected_runtime_run_id = \
            self._resolve_owned_runtime(user_id, workspace_id, run_id, runtime_run_id)
        summary, warming, missing = archive_role_summary(selected_runtime_run_id)
        return {
            "run_id": run["run_id"],
            "runtime_run_id": current_runtime_run_id,
            "selected_runtime_run_id": selected_runtime_run_id,
            "previous_runtime_run_ids": history_runtime_run_ids,
            "mixed_run": False,
            "trace_file": "decision_traces.jsonl",
            "missing": missing,
            "traces": [],
            "total": 0,
            "next_cursor": None,
            "warming": warming,
            "role_summary": summary,
        }

    def save_report(self, user_id, workspace_id, experiment_id, report):
        query = {"user_id": str(user_id), "workspace_id": str(workspace_id), "experiment_id": str(experiment_id)}
        existing = self._find_one("reports", query)
        if existing:
            return self._update("reports", query, {"report": deepcopy(report)})
        now = _now()
        return self._insert("reports", {**query, "report": deepcopy(report), "created_at": now, "updated_at": now})

    def get_report(self, user_id, workspace_id, experiment_id):
        return self._find_one("reports", {"user_id": str(user_id), "workspace_id": str(workspace_id), "experiment_id": str(experiment_id)})

    def acquire_gateway_lease(self, owner_id: str, *, ttl_seconds: float = 60.0, lease_key: str = "default") -> bool:
        now, expires = _now(), _now() + timedelta(seconds=max(5.0, float(ttl_seconds)))
        with self._lock:
            existing = next((item for item in self._memory["leases"] if item.get("lease_key") == lease_key), None)
            existing_expiry = _parse_time(existing.get("expires_at")) if existing else None
            if existing and existing.get("owner_id") != owner_id and existing_expiry and existing_expiry > now:
                return False
            if existing:
                existing.update({"owner_id": owner_id, "expires_at": expires, "updated_at": now})
            else:
                self._memory["leases"].append({"lease_key": lease_key, "owner_id": owner_id, "expires_at": expires, "created_at": now, "updated_at": now})
            self._flush("leases")
            return True

    def release_gateway_lease(self, owner_id: str, *, lease_key: str = "default") -> None:
        with self._lock:
            self._memory["leases"] = [item for item in self._memory["leases"] if not (item.get("lease_key") == lease_key and item.get("owner_id") == owner_id)]
            self._flush("leases")
