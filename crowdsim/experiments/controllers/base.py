"""Controller protocol and shared decision builder."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Protocol

from ..contracts import SCHEMA_VERSION, new_id, public_time, validate_action, validate_observation


class ControlPolicy(Protocol):
    controller_id: str
    version: str

    def reset(self, context: Mapping[str, Any] | None = None) -> None: ...
    def should_decide(self, observation: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> bool: ...
    def decide(self, observation: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> dict[str, Any]: ...
    def on_ack(self, acknowledgement: Mapping[str, Any]) -> None: ...
    def on_effect(self, evaluation: Mapping[str, Any]) -> Any: ...
    def snapshot_state(self) -> dict[str, Any]: ...


class BaseController:
    controller_id = "base"
    version = "1.0.0"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.context: dict[str, Any] = {}
        self.acks: list[dict[str, Any]] = []
        self.effects: list[dict[str, Any]] = []

    def reset(self, context: Mapping[str, Any] | None = None) -> None:
        self.context = dict(context or {})
        self.acks = []
        self.effects = []

    def on_ack(self, acknowledgement: Mapping[str, Any]) -> None:
        self.acks.append(deepcopy(dict(acknowledgement)))
        self.acks = self.acks[-200:]

    def on_effect(self, evaluation: Mapping[str, Any]) -> None:
        self.effects.append(deepcopy(dict(evaluation)))
        self.effects = self.effects[-100:]

    def snapshot_state(self) -> dict[str, Any]:
        return {"context": deepcopy(self.context), "acks": deepcopy(self.acks), "effects": deepcopy(self.effects)}

    def _decision(
        self,
        observation: Mapping[str, Any],
        actions: list[Mapping[str, Any]],
        reason: str,
        *,
        trigger: str,
        algorithm_trace: Mapping[str, Any] | None = None,
        model_trace: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        obs = validate_observation(observation)
        validity = float(self.config.get("actionValiditySeconds", 120.0))
        start = float(obs["simTimeSeconds"])
        return {
            "schemaVersion": SCHEMA_VERSION,
            "decisionId": new_id("dec"),
            "runId": obs["runId"],
            "observationId": obs["observationId"],
            "controllerId": self.controller_id,
            "controllerVersion": self.version,
            "trigger": {"type": trigger},
            "reason": str(reason),
            "actions": [validate_action(action) for action in actions],
            "expectedEffect": {"riskDirection": "decrease", "efficiencyDirection": "increase"},
            "validFrom": start,
            "validUntil": start + max(1.0, validity),
            "algorithmTrace": deepcopy(dict(algorithm_trace or {})),
            "modelTrace": deepcopy(dict(model_trace)) if model_trace is not None else None,
            "createdAt": public_time(),
        }
