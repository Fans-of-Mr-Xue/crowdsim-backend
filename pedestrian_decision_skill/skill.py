"""Standalone pedestrian decision skill.

The complete basic workflow is implemented without depending on CrowdSim's
Agent model, simulator, or transport layer, so a small external adapter can
choose when to request decisions and how to apply high-level actions.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from .contracts import (
    ALLOWED_ACTIONS,
    DENSITY_LEVELS,
    PedestrianDecisionContext,
    PedestrianDecisionResult,
)

if TYPE_CHECKING:
    from .client import DeepSeekClient


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _bounded_number(
    value: Any,
    *,
    default: float = 0.0,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    number = max(minimum, _number(value, default))
    return min(maximum, number) if maximum is not None else number


def normalize_context(context: dict) -> PedestrianDecisionContext:
    """Return a new v1 context containing only supported, normalized fields.

    Nested groups that are missing or are not dictionaries use their complete
    default values. Additional fields are intentionally ignored so future
    simulator payloads do not become part of the v1 model contract by accident.
    """
    if not isinstance(context, dict):
        raise TypeError("pedestrian decision context must be a dictionary")

    profile = _mapping(context.get("profile"))
    current_state = _mapping(context.get("current_state"))
    surrounding_crowd = _mapping(context.get("surrounding_crowd"))

    density_level = _text(
        surrounding_crowd.get("density_level"),
        "free",
    ).lower()
    if density_level not in DENSITY_LEVELS:
        density_level = "free"

    nearby_people = int(
        _bounded_number(surrounding_crowd.get("nearby_people"), minimum=0.0)
    )

    return {
        "agent_id": _text(context.get("agent_id"), ""),
        "profile": {
            "nationality": _text(profile.get("nationality"), "unknown"),
            "language": _text(profile.get("language"), "unknown"),
        },
        "current_state": {
            "status": _text(current_state.get("status"), "unknown"),
            "speed": _bounded_number(current_state.get("speed"), minimum=0.0),
            "stress": _bounded_number(
                current_state.get("stress"), minimum=0.0, maximum=1.0
            ),
            "fatigue": _bounded_number(
                current_state.get("fatigue"), minimum=0.0, maximum=1.0
            ),
            "flood_impact": _bounded_number(
                current_state.get("flood_impact"), minimum=0.0, maximum=1.0
            ),
            "event_impact": _bounded_number(
                current_state.get("event_impact"), minimum=0.0, maximum=1.0
            ),
        },
        "surrounding_crowd": {
            "nearby_people": nearby_people,
            "local_density": _bounded_number(
                surrounding_crowd.get("local_density"), minimum=0.0
            ),
            "density_level": density_level,
        },
    }


class PedestrianDecisionSkill:
    """Choose one high-level action for a pedestrian."""

    ALLOWED_ACTIONS = ALLOWED_ACTIONS

    def __init__(
        self,
        client: DeepSeekClient | None = None,
        *,
        config_path: str | Path | None = None,
    ) -> None:
        if client is not None and config_path is not None:
            raise ValueError("pass either client or config_path, not both")
        self._client = client
        self._config_path = config_path

    @staticmethod
    def normalize_context(context: dict) -> PedestrianDecisionContext:
        """Normalize simulator input without retaining or mutating it."""
        return normalize_context(context)

    @staticmethod
    def build_messages(context: dict) -> list[dict[str, str]]:
        """Build DeepSeek-compatible messages from raw simulator input."""
        from .prompts import build_messages

        return build_messages(context)

    @staticmethod
    def parse_decision(raw_output: str) -> PedestrianDecisionResult:
        """Validate raw model content and build a standardized LLM result."""
        from .validation import parse_decision

        return parse_decision(raw_output)

    async def decide(
        self,
        context: PedestrianDecisionContext,
    ) -> PedestrianDecisionResult:
        """Use DeepSeek when available, otherwise return a local safe decision."""
        from .client import DeepSeekClient, DeepSeekClientError
        from .config import DeepSeekConfigError
        from .fallback import fallback_decision
        from .prompts import build_messages
        from .validation import DecisionValidationError, parse_decision

        normalized = normalize_context(context)
        try:
            if self._client is None:
                self._client = DeepSeekClient(config_path=self._config_path)
            raw_output = await self._client.complete(build_messages(normalized))
            return parse_decision(raw_output)
        except (
            DeepSeekConfigError,
            DeepSeekClientError,
            DecisionValidationError,
        ):
            return fallback_decision(normalized)
