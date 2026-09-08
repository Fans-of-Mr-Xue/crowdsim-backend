"""FIFO commands that become effective only at a simulation boundary."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class RuntimeCommand:
    action: str
    payload: dict[str, Any]
    request_id: str | None
    submitted_at: float
    submitted_snapshot: str


class RuntimeCommandQueue:
    def __init__(self) -> None:
        self._pending: deque[RuntimeCommand] = deque()
        self._results: deque[dict[str, Any]] = deque()

    def submit(self, command: RuntimeCommand) -> None:
        self._pending.append(command)

    def apply_all(
        self,
        handler: Callable[[str, dict[str, Any]], Any],
        *,
        applied_at: float,
        snapshot_id: str,
    ) -> list[dict[str, Any]]:
        applied = []
        while self._pending:
            command = self._pending.popleft()
            result = {
                "type": "command_result",
                "request_id": command.request_id,
                "action": command.action,
                "status": "applied",
                "submitted_at": command.submitted_at,
                "applied_at": applied_at,
                "snapshot_id": snapshot_id,
            }
            try:
                detail = handler(command.action, command.payload)
                if detail is not None:
                    result["detail"] = detail
            except (RuntimeError, ValueError) as exc:
                result.update(status="rejected", code="invalid_command", message=str(exc))
            self._results.append(result)
            applied.append(result)
        return applied

    def take_results(self) -> list[dict[str, Any]]:
        results = list(self._results)
        self._results.clear()
        return results

    @property
    def pending_count(self) -> int:
        return len(self._pending)
