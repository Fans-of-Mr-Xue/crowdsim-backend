"""Controller registry for C0-C5."""

from __future__ import annotations

from typing import Any, Mapping

from .controllers import ArdeAdapter, LlmController, NoControl, RuleBasedController


class ControllerRegistry:
    CATALOG = {
        "C0": {"id": "no_control", "name": "No-Control", "actions": []},
        "C1": {"id": "rule_based", "name": "Rule-Based", "actions": ["publish_guidance", "set_inflow_rate", "set_edge_risk_weight"]},
        "C2": {"id": "llm_oneshot", "name": "LLM-OneShot", "actions": ["*"]},
        "C3": {"id": "llm_periodic", "name": "LLM-Periodic-30", "actions": ["*"]},
        "C4": {"id": "llm_event", "name": "LLM-Event", "actions": ["*"]},
        "C5": {"id": "arde_dynamic", "implementationId": "arde_dynamic_v1", "name": "ARDE-Dynamic", "actions": ["*"]},
    }

    def __init__(self, *, llm_client=None) -> None:
        self.llm_client = llm_client

    def catalog(self) -> list[dict[str, Any]]:
        return [{"methodId": method_id, **spec} for method_id, spec in self.CATALOG.items()]

    def create(self, method_id: str, config: Mapping[str, Any] | None = None):
        config = dict(config or {})
        if method_id == "C0":
            return NoControl(config)
        if method_id == "C1":
            return RuleBasedController(config)
        if method_id == "C2":
            return LlmController("oneshot", config, llm_client=self.llm_client)
        if method_id == "C3":
            return LlmController("periodic", config, llm_client=self.llm_client)
        if method_id == "C4":
            return LlmController("event", config, llm_client=self.llm_client)
        if method_id == "C5":
            return ArdeAdapter(config)
        raise ValueError(f"unknown control method: {method_id}")
