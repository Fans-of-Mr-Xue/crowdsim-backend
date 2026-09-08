import asyncio
import json
import os
import urllib.request
from typing import Any, Dict, Tuple

from crowdsim_models import Agent


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


class AgentDecisionEngine:
    """Optional OpenAI-compatible decision layer with a local fallback."""

    ACTIONS = {"continue", "slow_down", "avoid", "follow_crowd", "wait"}

    def __init__(self) -> None:
        self.endpoint = os.getenv("CROWDSIM_LLM_ENDPOINT", "").strip()
        self.api_key = os.getenv("CROWDSIM_LLM_API_KEY", "").strip()
        self.model = os.getenv("CROWDSIM_LLM_MODEL", "").strip()
        self.timeout = max(1.0, _env_float("CROWDSIM_LLM_TIMEOUT", 8.0))
        self.enabled = bool(self.endpoint and self.model)
        self.last_error = ""
        self.llm_decisions = 0
        self.fallback_decisions = 0

    def local_decision(self, agent: Agent) -> Tuple[str, str]:
        if agent.stress >= 0.82 or agent.density_level == "critical":
            return "avoid", "critical crowd pressure"
        if agent.flood_impact >= 0.35:
            return "avoid", "hazard exposure"
        if agent.event_avoidance >= 0.35 or agent.event_impact >= 0.45:
            return "avoid", "emergency event exposure"
        if agent.nearby_people > agent.acceptable_people or agent.density_level == "crowded":
            return "slow_down", "personal crowd threshold exceeded"
        if agent.stress >= 0.55 and agent.risk_tolerance < 0.45:
            return "wait", "stress exceeds risk tolerance"
        return "continue", "conditions acceptable"

    async def decide(self, agent: Agent) -> Tuple[str, str, str]:
        fallback_action, fallback_reason = self.local_decision(agent)
        if not self.enabled:
            self.fallback_decisions += 1
            return fallback_action, fallback_reason, "local"
        try:
            action, reason = await asyncio.to_thread(self._request, self._prompt(agent))
            self.llm_decisions += 1
            return action, reason, "llm"
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.fallback_decisions += 1
            return fallback_action, fallback_reason, "local_fallback"

    def _prompt(self, agent: Agent) -> Dict[str, Any]:
        return {
            "agent": {key: getattr(agent, key) for key in (
                "age_group", "mobility", "risk_tolerance", "familiarity",
                "group_size", "acceptable_people", "cultural_group",
                "personal_space", "density_tolerance", "barrier_compliance",
                "queue_mode", "shortest_path_weight", "low_density_path_weight",
                "following_tendency", "language_delay", "symbol_accuracy",
                "authority_compliance", "information_trust",
                "same_culture_attraction", "group_cohesion", "help_seeking",
                "conflict_threshold", "panic_susceptibility", "stress_response",
                "counterflow_tendency", "recovery_seconds",
            )},
            "environment": {key: getattr(agent, key) for key in (
                "nearby_people", "local_density", "density_level", "stress",
                "fatigue", "flood_impact", "event_impact", "event_avoidance", "event_phase",
            )},
            "allowed_actions": sorted(self.ACTIONS),
        }

    def _request(self, prompt: Dict[str, Any]) -> Tuple[str, str]:
        body = json.dumps({
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Decide one pedestrian action. Return JSON with action and a short reason."},
                {"role": "user", "content": json.dumps(prompt)},
            ],
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = json.loads(payload["choices"][0]["message"]["content"])
        action = str(result.get("action", "continue"))
        if action not in self.ACTIONS:
            raise ValueError(f"unsupported action: {action}")
        return action, str(result.get("reason", "model decision"))[:160]

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "llm_decisions": self.llm_decisions,
            "fallback_decisions": self.fallback_decisions,
            "last_error": self.last_error,
        }
