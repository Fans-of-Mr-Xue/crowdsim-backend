"""Strict validation for one raw model decision."""

import json
import math
from typing import Any, cast

from .contracts import ALLOWED_ACTIONS, PedestrianAction, PedestrianDecisionResult


DECISION_FIELDS = frozenset({"action", "reason", "confidence"})
MAX_REASON_LENGTH = 80


class DecisionValidationError(ValueError):
    """The model output does not satisfy the v1 decision contract."""


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DecisionValidationError(
                f"model output contains duplicate field '{key}'"
            )
        result[key] = value
    return result


def parse_decision(raw_output: str) -> PedestrianDecisionResult:
    """Parse DeepSeek content and return a validated v1 LLM decision."""
    if not isinstance(raw_output, str):
        raise DecisionValidationError("model output must be a string")
    if not raw_output.strip():
        raise DecisionValidationError("model output must not be empty")

    try:
        data = json.loads(raw_output, object_pairs_hook=_json_object)
    except json.JSONDecodeError as exc:
        raise DecisionValidationError("model output is not valid JSON") from exc

    if not isinstance(data, dict):
        raise DecisionValidationError("model output must be a JSON object")

    fields = set(data)
    if fields != DECISION_FIELDS:
        missing = sorted(DECISION_FIELDS - fields)
        extra = sorted(fields - DECISION_FIELDS)
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected fields: {', '.join(extra)}")
        raise DecisionValidationError("; ".join(details))

    action = data["action"]
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        allowed = ", ".join(sorted(ALLOWED_ACTIONS))
        raise DecisionValidationError(
            f"action must be one of: {allowed}"
        )

    reason = data["reason"]
    if not isinstance(reason, str):
        raise DecisionValidationError("reason must be a string")
    reason = reason.strip()
    if not reason:
        raise DecisionValidationError("reason must not be empty")
    if len(reason) > MAX_REASON_LENGTH:
        raise DecisionValidationError(
            f"reason must not exceed {MAX_REASON_LENGTH} characters"
        )

    confidence = data["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise DecisionValidationError("confidence must be a number")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise DecisionValidationError(
            "confidence must be a finite number between 0 and 1"
        )

    return {
        "action": cast(PedestrianAction, action),
        "reason": reason,
        "confidence": confidence,
        "source": "llm",
    }
