"""C5 adapter for the ARDE package shipped in this repository."""

from __future__ import annotations

from typing import Any, Mapping


def _load_arde():
    from arde_emergency import ArdeController

    return ArdeController


class ArdeAdapter:
    controller_id = "arde_dynamic"
    version = "1.0.0"

    def __init__(self, config: Mapping[str, Any] | None = None, *, llm_provider=None) -> None:
        raw = dict(config or {})
        aliases = {
            "actionValiditySeconds": "action_validity_seconds",
            "decisionCooldownSeconds": "decision_cooldown_seconds",
            "randomSeed": "random_seed",
            "llmEnabled": "llm_enabled",
            "llmTimeoutSeconds": "llm_timeout_seconds",
            "ablationMode": "ablation_mode",
        }
        normalized = {aliases.get(key, key): value for key, value in raw.items()}
        allowed = {
            "version", "learning_rate", "discount_factor", "initial_exploration", "min_exploration",
            "max_exploration", "entropy_target_low", "entropy_target_high", "gini_limit", "risk_high",
            "outer_step", "decision_cooldown_seconds", "action_validity_seconds", "llm_enabled",
            "llm_timeout_seconds", "random_seed", "ablation_mode", "reward",
        }
        normalized = {key: value for key, value in normalized.items() if key in allowed}
        self._controller = _load_arde()(normalized, llm_provider=llm_provider)

    def reset(self, context=None):
        return self._controller.reset(context)

    def should_decide(self, observation, context=None):
        return self._controller.should_decide(observation, context)

    def decide(self, observation, context=None):
        return self._controller.decide(observation, context)

    def on_ack(self, acknowledgement):
        return self._controller.on_ack(acknowledgement)

    def on_effect(self, evaluation):
        return self._controller.on_effect(evaluation)

    def snapshot_state(self):
        return self._controller.snapshot_state()
