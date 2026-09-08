"""Provider-neutral extension point for a future pedestrian decision model."""

from __future__ import annotations

from typing import Protocol


class LlmGateway(Protocol):
    """An integration supplied by the application must return one JSON object."""

    model_id: str

    def complete(self, context: dict) -> dict:
        ...
