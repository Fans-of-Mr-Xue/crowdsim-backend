"""Atomic, immutable JSON storage for accepted simulation requirements."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from crowdsim.domain.requirement_spec import RequirementSpec


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENT_ID_PATTERN = re.compile(r"^req-(?:[0-9a-f]{32}|default)$")


class RequirementNotFoundError(FileNotFoundError):
    pass


class RequirementRepository:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or PROJECT_ROOT / "runs" / "requirements")

    def create(self, spec: RequirementSpec, *, purpose: str | None = None) -> dict:
        spec = RequirementSpec.parse(spec.payload, allow_empty_population=purpose == "experiment")
        self.root.mkdir(parents=True, exist_ok=True)
        for _ in range(4):
            requirement_id = f"req-{uuid.uuid4().hex}"
            destination = self.root / f"{requirement_id}.json"
            if destination.exists():
                continue
            now = datetime.now(timezone.utc).isoformat()
            record = {
                "requirement_id": requirement_id,
                "schema_version": spec.payload["schema_version"],
                "created_at": now,
                "status": "accepted",
                "fingerprint": f"sha256:{spec.fingerprint}",
                "requirement": spec.payload,
                "capabilities": spec.capabilities(),
            }
            if purpose is not None:
                record["purpose"] = purpose
            temporary = destination.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(destination)
            return record
        raise FileExistsError("could not allocate a unique requirement id")

    def load(self, requirement_id: str) -> dict:
        if not isinstance(requirement_id, str) or not REQUIREMENT_ID_PATTERN.fullmatch(requirement_id):
            raise RequirementNotFoundError("invalid requirement id")
        path = self.root / f"{requirement_id}.json"
        if not path.is_file():
            raise RequirementNotFoundError(f"unknown requirement: {requirement_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("requirement_id") != requirement_id:
            raise ValueError(f"requirement id mismatch: {requirement_id}")
        spec = RequirementSpec.parse(
            record.get("requirement"), allow_empty_population=record.get("purpose") == "experiment",
        )
        if record.get("fingerprint") != f"sha256:{spec.fingerprint}":
            raise ValueError(f"requirement fingerprint mismatch: {requirement_id}")
        # Refresh current code capabilities in memory; preserve immutable files.
        record["capabilities"] = spec.capabilities()
        return record
