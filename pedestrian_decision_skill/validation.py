"""Strict validation for one raw DeepSeek decision."""

import json
import math
from typing import Any, Mapping, cast

from .contracts import ALLOWED_ACTIONS, PedestrianAction, PedestrianDecisionResult


DECISION_FIELDS = frozenset({"action", "target_id", "reason", "confidence"})
MAX_REASON_LENGTH = 80


class DecisionValidationError(ValueError):
    pass


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DecisionValidationError(f"model output contains duplicate field '{key}'")
        result[key] = value
    return result


def parse_decision(
    raw_output: str,
    candidate_targets: Mapping[str, str] | None = None,
) -> PedestrianDecisionResult:
    if not isinstance(raw_output, str) or not raw_output.strip():
        raise DecisionValidationError("model output must be a non-empty string")
    try:
        data = json.loads(raw_output, object_pairs_hook=_json_object)
    except json.JSONDecodeError as exc:
        raise DecisionValidationError("model output is not valid JSON") from exc
    if not isinstance(data, dict):
        raise DecisionValidationError("model output must be a JSON object")
    if set(data) != DECISION_FIELDS:
        missing = sorted(DECISION_FIELDS - set(data))
        extra = sorted(set(data) - DECISION_FIELDS)
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected fields: {', '.join(extra)}")
        raise DecisionValidationError("; ".join(details))

    action = data["action"]
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise DecisionValidationError(
            f"action must be one of: {', '.join(sorted(ALLOWED_ACTIONS))}"
        )

    target_id = data["target_id"]
    if target_id is not None and (not isinstance(target_id, str) or not target_id.strip()):
        raise DecisionValidationError("target_id must be null or a non-empty string")
    if isinstance(target_id, str):
        target_id = target_id.strip()
    targets = dict(candidate_targets or {})
    if action in {"reroute", "change_goal"}:
        if target_id not in targets:
            raise DecisionValidationError("target_id must reference an available candidate")
        if action == "change_goal" and targets[target_id] != "activity":
            raise DecisionValidationError("change_goal requires an activity candidate")
    elif target_id is not None:
        raise DecisionValidationError(f"{action} requires target_id to be null")

    reason = data["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise DecisionValidationError("reason must be a non-empty string")
    reason = reason.strip()
    if len(reason) > MAX_REASON_LENGTH:
        raise DecisionValidationError(
            f"reason must not exceed {MAX_REASON_LENGTH} characters"
        )

    confidence = data["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise DecisionValidationError("confidence must be a number")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise DecisionValidationError("confidence must be between 0 and 1")

    return {
        "action": cast(PedestrianAction, action),
        "target_id": target_id,
        "reason": reason,
        "confidence": confidence,
        "source": "llm",
    }
