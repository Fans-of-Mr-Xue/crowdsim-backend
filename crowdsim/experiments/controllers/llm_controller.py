"""Structured LLM controller for C2, C3 and C4."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping

from .base import BaseController
from .rule_based import RuleBasedController
from ..llm_client import ControlLlmClient


def _extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else value[3:]
        value = value.rsplit("```", 1)[0]
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM response contains no JSON object")
    parsed = json.loads(value[start:end + 1])
    if not isinstance(parsed, dict) or not isinstance(parsed.get("actions"), list):
        raise ValueError("LLM response must contain actions array")
    return parsed


class LlmController(BaseController):
    version = "1.0.0"

    def __init__(
        self,
        mode: str,
        config: Mapping[str, Any] | None = None,
        *,
        llm_call: Callable[..., tuple[str, dict[str, Any]]] | None = None,
        llm_client: ControlLlmClient | None = None,
    ) -> None:
        super().__init__(config)
        if mode not in {"oneshot", "periodic", "event"}:
            raise ValueError("LLM controller mode is invalid")
        self.mode = mode
        self.controller_id = f"llm_{mode}"
        self._llm_client = llm_client or ControlLlmClient.from_config(self.config)
        self.llm_call = llm_call or self._llm_client.complete
        self._last_decision_time: float | None = None
        self._has_decided = False
        self._fallback = RuleBasedController(config)

    def reset(self, context=None) -> None:
        super().reset(context)
        self._last_decision_time = None
        self._has_decided = False
        self._fallback.reset(context)

    def should_decide(self, observation, context=None) -> bool:
        now = float(observation.get("simTimeSeconds") or 0.0)
        if self.mode == "oneshot":
            return not self._has_decided
        if self.mode == "periodic":
            period = max(5.0, float(self.config.get("periodSeconds", 30.0)))
            return self._last_decision_time is None or now - self._last_decision_time >= period
        trend = float((observation.get("trend") or {}).get("risk") or 0.0)
        risk = float((observation.get("global") or {}).get("riskIndex") or 0.0)
        hazards = observation.get("hazards") or []
        hazard_active = any(
            isinstance(item, Mapping)
            and bool(item.get("active", True))
            and float(item.get("intensity") or item.get("risk") or 0.0) > 0
            for item in hazards
        )
        triggered = (
            hazard_active
            or trend >= float(self.config.get("eventRiskDelta", 0.05))
            or risk >= float(self.config.get("eventRiskThreshold", 0.30))
        )
        cooldown = max(5.0, float(self.config.get("eventCooldownSeconds", 60.0)))
        return triggered and (self._last_decision_time is None or now - self._last_decision_time >= cooldown)

    def decide(self, observation, context=None):
        started = time.perf_counter()
        prompt = {
            "task": "Choose only supported crowd-control actions. Return JSON with reason and actions.",
            "allowedActionTypes": ["observe_only", "publish_guidance", "set_inflow_rate", "set_edge_capacity", "set_route_distribution", "set_edge_risk_weight"],
            "observation": observation,
            "capabilities": (context or {}).get("capabilities", {}),
        }
        messages = [
            {"role": "system", "content": "You are a crowd-control decision module. Output one JSON object only."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ]
        self._has_decided = True
        self._last_decision_time = float(observation.get("simTimeSeconds") or 0.0)
        try:
            text, metadata = self.llm_call(
                messages,
                timeout=self.config.get("timeoutSeconds", 30),
                model=self.config.get("model"),
                max_tokens=self.config.get("maxTokens", 1200),
            )
            parsed = _extract_json(text)
            return self._decision(
                observation,
                parsed["actions"],
                str(parsed.get("reason") or "LLM structured decision"),
                trigger=self.mode,
                model_trace={
                    "status": "accepted",
                    "durationMs": int((time.perf_counter() - started) * 1000),
                    "model": metadata.get("model") or self.config.get("model"),
                    "raw": text[:8000],
                    **dict(metadata or {}),
                },
            )
        except Exception as exc:
            fallback = self._fallback.decide(observation, {"trigger": f"{self.mode}_fallback"})
            fallback["controllerId"] = self.controller_id
            fallback["modelTrace"] = {
                "status": "fallback",
                "durationMs": int((time.perf_counter() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            }
            return fallback
