from __future__ import annotations

from typing import Any, Mapping

from .base import BaseController


class NoControl(BaseController):
    controller_id = "no_control"

    def should_decide(self, observation: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> bool:
        return False

    def decide(self, observation: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._decision(observation, [], "C0 records observations without control", trigger="disabled")
