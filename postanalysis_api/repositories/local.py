"""Owner-scoped JSON snapshots with append-only JSONL activity records."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from threading import RLock
from typing import Any
from uuid import uuid4

from postanalysis_api.schemas.common import ApiError


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class LocalRepository:
    KINDS = {"datasets": "ds", "simulations": "sim", "experiments": "exp", "runs": "run", "results": "result"}

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()

    def _path(self, kind: str, item_id: str) -> Path:
        if kind not in self.KINDS or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item_id) is None:
            raise ApiError("VALIDATION_ERROR", "资源路径无效", 400)
        return self.root / kind / f"{item_id}.json"

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)

    def create(self, kind: str, user: str, workspace: str, payload: dict) -> dict:
        with self.lock:
            item_id = f"{self.KINDS[kind]}_{uuid4().hex}"
            document = {"id": item_id, "ownerId": user, "workspaceId": workspace,
                        "createdAt": now(), "updatedAt": now(), **deepcopy(payload)}
            self._write(self._path(kind, item_id), document)
            return deepcopy(document)

    def get(self, kind: str, item_id: str, user: str, workspace: str) -> dict:
        path = self._path(kind, item_id)
        with self.lock:
            if not path.is_file():
                raise ApiError("NOT_FOUND", "资源不存在", 404)
            document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("ownerId") != user or document.get("workspaceId") != workspace:
            raise ApiError("RESOURCE_FORBIDDEN", "当前工作区无权访问此资源", 403)
        return document

    def update(self, kind: str, item_id: str, user: str, workspace: str, changes: dict) -> dict:
        with self.lock:
            document = self.get(kind, item_id, user, workspace)
            document.update(deepcopy(changes))
            document["updatedAt"] = now()
            self._write(self._path(kind, item_id), document)
            return deepcopy(document)

    def list(self, kind: str, user: str, workspace: str, *, limit: int = 50) -> list[dict]:
        if kind not in self.KINDS:
            raise ValueError(kind)
        folder = self.root / kind
        with self.lock:
            items = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("*.json")] if folder.exists() else []
        return sorted((item for item in items if item.get("ownerId") == user and item.get("workspaceId") == workspace),
                      key=lambda item: item["createdAt"], reverse=True)[:limit]

    def event(self, kind: str, item_id: str, event_type: str, detail: dict | None = None) -> None:
        path = self._path(kind, item_id).with_suffix(".events.jsonl")
        entry = {"at": now(), "type": event_type, "detail": detail or {}}
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")

    def events(self, kind: str, item_id: str, user: str, workspace: str, after: int, limit: int = 200) -> dict:
        self.get(kind, item_id, user, workspace)
        path = self._path(kind, item_id).with_suffix(".events.jsonl")
        with self.lock:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return {"events": [{"sequence": index + 1, **json.loads(line)} for index, line in
                           enumerate(lines[after:after + limit], start=after)],
                "nextAfter": min(len(lines), after + limit)}

    def idempotent(self, user: str, workspace: str, method: str, path: str,
                   key: str, body: dict, operation) -> tuple[int, dict, bool]:
        import hashlib
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                                separators=(",", ":")).encode("utf-8")).hexdigest()
        lookup = hashlib.sha256(f"{user}\n{workspace}\n{method}\n{path}\n{key}".encode("utf-8")).hexdigest()
        record_path = self.root / "idempotency" / f"{lookup}.json"
        with self.lock:
            if record_path.exists():
                saved = json.loads(record_path.read_text(encoding="utf-8"))
                if saved["fingerprint"] != fingerprint:
                    raise ApiError("IDEMPOTENCY_CONFLICT", "相同幂等键对应了不同请求内容", 409)
                if saved.get("state") == "in_progress":
                    raise ApiError("REQUEST_IN_PROGRESS", "同一请求仍在处理或需人工核对上次结果", 409)
                return saved["status"], saved["data"], True
            self._write(record_path, {"fingerprint": fingerprint, "state": "in_progress", "at": now()})
            try:
                status, data = operation()
            except (ApiError, ValueError):
                record_path.unlink(missing_ok=True)
                raise
            self._write(record_path, {"fingerprint": fingerprint, "state": "completed",
                                      "status": status, "data": data, "at": now()})
            return status, data, False

    def reconcile_interrupted(self) -> None:
        """A restarted process cannot claim that an old local worker is alive."""
        with self.lock:
            for kind in ("simulations", "experiments"):
                folder = self.root / kind
                if not folder.exists():
                    continue
                for path in folder.glob("*.json"):
                    document = json.loads(path.read_text(encoding="utf-8"))
                    if document.get("status") in {"queued", "running", "cancelling"}:
                        document["status"] = "interrupted"
                        document["updatedAt"] = now()
                        self._write(path, document)
                        self.event(kind, document["id"], "interrupted", {"reason": "PROCESS_RESTART"})
