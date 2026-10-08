"""Small HTTP routing primitives; business rules stay in services/schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from postanalysis_api.schemas.common import ApiError, reject_secrets, text_


PREFIX = "/api/v1/post"


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    body: dict
    headers: Any
    app: Any

    @property
    def user(self) -> str:
        return text_(self.headers.get("X-CrowdSim-User"), "X-CrowdSim-User",
                     max_len=64, pattern=r"[A-Za-z0-9][A-Za-z0-9_-]*")

    @property
    def workspace(self) -> str:
        return text_(self.headers.get("X-CrowdSim-Workspace"), "X-CrowdSim-Workspace",
                     max_len=64, pattern=r"[A-Za-z0-9][A-Za-z0-9_-]*")

    def mutation(self, operation) -> tuple[int, dict, bool]:
        key = text_(self.headers.get("Idempotency-Key"), "Idempotency-Key", max_len=128,
                    pattern=r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
        reject_secrets(self.body)
        return self.app.repo.idempotent(self.user, self.workspace, self.method,
                                        self.path, key, self.body, operation)


def response(data: dict | list, status: int = 200, replayed: bool = False) -> tuple[int, dict, bool]:
    return status, data, replayed

