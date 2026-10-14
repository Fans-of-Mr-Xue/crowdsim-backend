"""Immutable JSON repository for published artificial-society model versions."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from crowdsim.domain.artificial_society_spec import ArtificialSocietySpec
from crowdsim.infrastructure.requirement_repository import RequirementRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_ID_PATTERN = re.compile(r"^asm-[0-9a-f]{32}$")


class ArtificialSocietyNotFoundError(FileNotFoundError):
    pass


class ArtificialSocietyRepository:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        requirements: RequirementRepository | None = None,
    ) -> None:
        self.root = Path(root or PROJECT_ROOT / "runs" / "artificial_societies")
        self.requirements = requirements or RequirementRepository()

    def create(self, spec: ArtificialSocietySpec) -> dict:
        requirement = self.requirements.load(spec.requirement_id)
        expected_fingerprint = str(spec.payload.get("requirement_fingerprint") or "")
        if expected_fingerprint and expected_fingerprint != str(requirement.get("fingerprint") or ""):
            raise ValueError("requirement fingerprint mismatch")
        requirement_payload = requirement["requirement"]
        if spec.payload["individual_model"]["population_total"] != requirement_payload["population"]["total"]:
            raise ValueError("artificial society population must match requirement")
        if spec.payload["individual_model"]["distributions"] != requirement_payload["population"]["distributions"]:
            raise ValueError("artificial society distributions must match requirement")
        if spec.payload["environment_model"]["scenario_binding"]["location_id"] != requirement_payload["spatial_scope"]["location_id"]:
            raise ValueError("artificial society location must match requirement")

        self.root.mkdir(parents=True, exist_ok=True)
        for _ in range(4):
            model_id = f"asm-{uuid.uuid4().hex}"
            destination = self.root / f"{model_id}.json"
            if destination.exists():
                continue
            now = datetime.now(timezone.utc).isoformat()
            record = {
                "model_id": model_id,
                "schema_version": spec.payload["schema_version"],
                "created_at": now,
                "status": "published",
                "source": "manual",
                "fingerprint": f"sha256:{spec.fingerprint}",
                "requirement_id": spec.requirement_id,
                "artificial_society": spec.payload,
                "capabilities": spec.capabilities(),
            }
            temporary = destination.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(destination)
            return record
        raise FileExistsError("could not allocate a unique artificial society model id")

    def load(self, model_id: str) -> dict:
        if not isinstance(model_id, str) or not MODEL_ID_PATTERN.fullmatch(model_id):
            raise ArtificialSocietyNotFoundError("invalid artificial society model id")
        path = self.root / f"{model_id}.json"
        if not path.is_file():
            raise ArtificialSocietyNotFoundError(f"unknown artificial society model: {model_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        payload = record.get("artificial_society")
        if isinstance(payload, dict) and payload.get("schema_version") == 1:
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        else:
            fingerprint = ArtificialSocietySpec.parse(payload).fingerprint
        if record.get("fingerprint") != f"sha256:{fingerprint}":
            raise ValueError(f"artificial society fingerprint mismatch: {model_id}")
        return record
