"""Executable individual-model algorithm pipeline selected by crowd role."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.decision.route_provider import RouteCandidate
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, BehaviorPlan, Observation
from crowdsim.modeling_algorithm.contracts import Experience, GameChoice, PADState
from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
from crowdsim.modeling_algorithm.individual_model.desire_goal.emotion_driven_desire import EmotionDrivenDesireModel
from crowdsim.modeling_algorithm.individual_model.emotion.pad_continuous import PADContinuousEmotionModel
from crowdsim.modeling_algorithm.individual_model.game.bounded_rationality import BoundedRationalGameModel
from crowdsim.modeling_algorithm.individual_model.learning.experience_replay import ExperienceReplayBuffer


IMPLEMENTED_CHAIN = {
    "emotion": "PAD 连续情感模型",
    "desire": "情感驱动欲望",
    "game": "有限理性博弈",
    "decision": "LLM-driven",
    "learning": "经验回放",
}


@dataclass(frozen=True)
class RolePipelineConfig:
    role_id: str
    methods: dict[str, str]
    capabilities: dict[str, bool]

    @property
    def enabled(self) -> bool:
        required_capabilities = ("emotion", "game", "learning")
        return (
            all(self.capabilities.get(name, False) for name in required_capabilities)
            and all(self.methods.get(module_id) == method for module_id, method in IMPLEMENTED_CHAIN.items())
        )


class IndividualModelingEngine:
    """DecisionScheduler-compatible engine for configured artificial societies."""

    def __init__(self, fallback_engine: Any | None = None) -> None:
        self.fallback_engine = fallback_engine or AgentDecisionEngine()
        self.emotion = PADContinuousEmotionModel()
        self.desire = EmotionDrivenDesireModel()
        self.game = BoundedRationalGameModel()
        self.llm = LLMDrivenDecisionModel()
        self.replay = ExperienceReplayBuffer()
        self.role_configs: dict[str, RolePipelineConfig] = {}
        self.emotional_states: dict[str, PADState] = {}
        self.pipeline_decisions = 0
        self.delegated_decisions = 0
        self.fallback_decisions = 0
        self.enabled = bool(getattr(self.fallback_engine, "enabled", False))
        self.model = str(getattr(self.fallback_engine, "model", "") or self.llm.model)

    def configure(self, agent_types: list[dict[str, Any]]) -> None:
        configured: dict[str, RolePipelineConfig] = {}
        for agent in agent_types:
            role_id = str(agent.get("id") or "")
            methods = {
                str(item.get("id")): str(item.get("method") or "")
                for item in list(agent.get("architecture") or [])
                if isinstance(item, dict) and item.get("selected") is not False
            }
            configured[role_id] = RolePipelineConfig(
                role_id=role_id,
                methods=methods,
                capabilities={
                    str(name): bool(enabled)
                    for name, enabled in dict(agent.get("capabilities") or {}).items()
                },
            )
        self.role_configs = configured
        self.enabled = (
            any(config.enabled for config in configured.values())
            or bool(getattr(self.fallback_engine, "enabled", False))
        )

    def supports(self, profile: AgentProfile) -> bool:
        config = self.role_configs.get(profile.crowd_role)
        return bool(config and config.enabled)

    def rule_plan(
        self,
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        candidates: Iterable[RouteCandidate] = (),
    ) -> BehaviorPlan:
        candidates = tuple(candidates)
        if not self.supports(profile):
            self.delegated_decisions += 1
            return self.fallback_engine.rule_plan(profile, state, observation, candidates)
        emotion, desire, choice = self._evaluate(profile, state, observation, candidates)
        plan = self._choice_to_plan(profile, observation, candidates, choice, source="bounded_rational")
        self._remember(profile, observation, emotion, desire, plan)
        self.pipeline_decisions += 1
        return plan

    async def decide(
        self,
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        candidates: Iterable[RouteCandidate] = (),
    ) -> BehaviorPlan:
        candidates = tuple(candidates)
        if not self.supports(profile):
            self.delegated_decisions += 1
            return await self.fallback_engine.decide(profile, state, observation, candidates)
        emotion, desire, choice = self._evaluate(profile, state, observation, candidates)
        plan = await self.llm.decide(profile, state, observation, candidates, emotion, desire, choice)
        self._remember(profile, observation, emotion, desire, plan)
        self.pipeline_decisions += 1
        return plan

    def _evaluate(self, profile, state, observation, candidates):
        previous = self.emotional_states.get(profile.person_id)
        emotion = self.emotion.update(profile, state, observation, previous)
        self.emotional_states[profile.person_id] = emotion
        desire = self.desire.generate(emotion, profile, state, observation)
        replay_values = self.replay.action_values(role_id=profile.crowd_role)
        action_bias = {
            action: max(-0.2, min(0.2, reward * 0.1))
            for action, reward in replay_values.items()
        }
        choice = self.game.choose(profile, state, observation, emotion, desire, candidates, action_bias)
        return emotion, desire, choice

    @staticmethod
    def _choice_to_plan(profile, observation, candidates, choice: GameChoice, *, source: str) -> BehaviorPlan:
        candidate = next((item for item in candidates if item.target_id == choice.target_id), None)
        action = choice.action if choice.action not in {"reroute", "change_goal"} or candidate is not None else "continue"
        return LLMDrivenDecisionModel._make_plan(
            profile, observation, action, candidate,
            reason=choice.reason,
            confidence=None,
            source=source,
        )

    def _remember(self, profile, observation, emotion, desire, plan) -> None:
        speed_ratio = observation.own_motion.speed / max(0.1, profile.free_walking_speed * profile.mobility)
        reward = (
            min(1.0, speed_ratio)
            - observation.perceived_risk
            - 0.5 * observation.perceived_crowding
        )
        self.replay.append(Experience(
            person_id=profile.person_id,
            snapshot_id=observation.snapshot_id,
            role_id=profile.crowd_role,
            emotion=emotion.as_dict(),
            desire=desire.as_dict(),
            action=plan.proposed_action,
            target_id=plan.target_id,
            reward=reward,
            metadata={"source": plan.source, "time_seconds": observation.time_seconds},
        ))

    def diagnostics(self) -> dict[str, Any]:
        fallback = self.fallback_engine.diagnostics() if hasattr(self.fallback_engine, "diagnostics") else {}
        return {
            "enabled": self.enabled,
            "model": self.model,
            "last_error": str(fallback.get("last_error") or self.llm.last_error),
            "llm_decisions": int(fallback.get("llm_decisions", 0)) + self.llm.successes,
            "backend": "modeling_algorithm.individual_model.pipeline",
            "implemented_chain": dict(IMPLEMENTED_CHAIN),
            "configured_roles": {
                role_id: {
                    "enabled": config.enabled,
                    "methods": dict(config.methods),
                    "capabilities": dict(config.capabilities),
                }
                for role_id, config in self.role_configs.items()
            },
            "pipeline_decisions": self.pipeline_decisions,
            "delegated_decisions": self.delegated_decisions,
            "fallback_decisions": self.fallback_decisions,
            "experience_replay": self.replay.diagnostics(),
            "llm": self.llm.diagnostics(),
            "fallback_engine": fallback,
        }
