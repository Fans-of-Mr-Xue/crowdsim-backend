"""Executable individual-model algorithm pipeline selected by crowd role."""

from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
import time
from typing import Any, Iterable
from traci import constants as tc

from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.decision.route_provider import RouteCandidate, bind_candidates, select_candidate
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, BehaviorPlan, Observation
from crowdsim.modeling_algorithm.contracts import DesireState, Experience, GameChoice, PADState
from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
from crowdsim.modeling_algorithm.individual_model.desire_goal.emotion_driven_desire import EmotionDrivenDesireModel
from crowdsim.modeling_algorithm.individual_model.emotion.pad_continuous import PADContinuousEmotionModel
from crowdsim.modeling_algorithm.individual_model.game.bounded_rationality import BoundedRationalGameModel
from crowdsim.modeling_algorithm.individual_model.learning.experience_replay import ExperienceReplayBuffer
from crowdsim.modeling_algorithm.individual_model.desire_goal.runtime_goal import goal_intent
from crowdsim.modeling_algorithm.individual_model.module_assembly import assemble_roles


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
        return any(self.capabilities.get(key, False) for key in ("emotion", "game", "learning")) or self.methods.get("decision") == "LLM-driven"

    @property
    def desire_enabled(self) -> bool:
        return self.capabilities.get("emotion", False) and self.methods.get("desire") == "情感驱动欲望"

    @property
    def replay_enabled(self) -> bool:
        return self.capabilities.get("learning", False) and self.methods.get("learning") == "经验回放"


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
        self.assemblies = {}
        self.execution_contract = None
        self._boundary_contexts = {}
        self._affect_contexts: dict[str, dict[str, Any]] = {}
        self._goal_contexts = {}
        self._generation = 0
        self._decision_contexts = {}
        self._candidate_scores: dict[str, dict[str, dict[str, Any]]] = {}
        self._choice_contexts: dict[str, dict[str, Any]] = {}
        self.emotional_states: dict[str, PADState] = {}
        self.pipeline_decisions = 0
        self.delegated_decisions = 0
        self.fallback_decisions = 0
        self.enabled = bool(getattr(self.fallback_engine, "enabled", False))
        self.model = str(getattr(self.fallback_engine, "model", "") or self.llm.model)

    def configure(self, agent_types: list[dict[str, Any]], *, execution_contract: str | None = None) -> None:
        assemblies = assemble_roles(agent_types, execution_contract)
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
        previous_contract = self.execution_contract
        self._generation += 1
        self.role_configs = configured
        self.assemblies = assemblies
        self.execution_contract = execution_contract
        self._boundary_contexts = {}
        self._affect_contexts = {}
        self._goal_contexts = {}
        self._decision_contexts = {}
        self._candidate_scores = {}
        self._choice_contexts = {}
        self.emotional_states = {}
        if execution_contract is not None or previous_contract is not None:
            self.replay = ExperienceReplayBuffer()
            self.pipeline_decisions = self.delegated_decisions = self.fallback_decisions = 0
        self.enabled = (
            any(config.methods.get("decision") == "LLM-driven" for config in configured.values())
            or bool(getattr(self.fallback_engine, "enabled", False))
        )

    def invalidate_decisions(self):
        self._generation += 1

    def retain_active_people(self, person_ids: Iterable[str]) -> None:
        if self.execution_contract is None:
            return
        active = set(person_ids)
        self._boundary_contexts = {pid: value for pid, value in self._boundary_contexts.items() if pid in active}
        self._affect_contexts = {pid: value for pid, value in self._affect_contexts.items() if pid in active}
        self._goal_contexts = {pid: value for pid, value in self._goal_contexts.items() if pid in active}
        self._decision_contexts = {pid: value for pid, value in self._decision_contexts.items() if pid in active}
        self._candidate_scores = {pid: value for pid, value in self._candidate_scores.items() if pid in active}
        self._choice_contexts = {pid: value for pid, value in self._choice_contexts.items() if pid in active}
        self.emotional_states = {pid: value for pid, value in self.emotional_states.items() if pid in active}

    def supports(self, profile: AgentProfile) -> bool:
        config = self.role_configs.get(profile.crowd_role)
        return bool(config and config.enabled)

    def llm_eligible(self, profile, state, observation, candidates=()):
        config = self.role_configs.get(profile.crowd_role)
        if config:
            assembly = self.assemblies.get(profile.crowd_role)
            decision = getattr(assembly, 'decision', None) if assembly is not None else None
            callable_decision = getattr(decision, 'callable', None)
            return config.methods.get('decision') == 'LLM-driven' and (
                callable_decision if callable_decision is not None else decision is not None)
        if self.execution_contract is not None:
            return False
        return bool(getattr(self.fallback_engine, 'enabled', False)) and not any(c.target_kind == 'hotspot_route' for c in candidates)

    def prepare_decision_boundary(self, profile, state, observation, candidates=()):
        if self.execution_contract not in {'candidate-decision-v1', 'heterogeneous-learning-v2'}:
            return tuple(candidates)
        key = (observation.snapshot_id, observation.time_seconds)
        cached = self._decision_contexts.get(profile.person_id)
        if not cached or cached[0] != key:
            # New ID is bound to the complete payload; no arbitrary user supplied ID survives.
            bound = bind_candidates(candidates, profile.person_id, observation.snapshot_id)
            self._decision_contexts[profile.person_id] = (key, deepcopy((profile, state, observation, bound)))
        return deepcopy(self._decision_contexts[profile.person_id][1][3])

    def _decision_inputs(self, profile, state, observation, candidates):
        if self.execution_contract in {'candidate-decision-v1', 'heterogeneous-learning-v2'}:
            self.prepare_decision_boundary(profile, state, observation, candidates)
            return deepcopy(self._decision_contexts[profile.person_id][1])
        return profile, state, observation, self._frozen_candidates(profile, observation, candidates)

    @staticmethod
    def _candidate_for_plan(plan, candidates):
        if plan.proposed_action not in {'reroute', 'change_goal'}:
            return None
        if plan.candidate_id is not None:
            return select_candidate(candidates, candidate_id=plan.candidate_id, target_id=plan.target_id)
        matches = [c for c in candidates if c.target_id == plan.target_id and c.edges == plan.route_edges
                   and c.arrival_position == plan.arrival_position
                   and (c.activity_duration if plan.proposed_action == 'change_goal' else None) == plan.activity_duration
                   and (c.next_route_edges if plan.proposed_action == 'change_goal' else ()) == plan.next_route_edges
                   and (c.next_arrival_position if plan.proposed_action == 'change_goal' else None) == plan.next_arrival_position
                   and (c.next_target_id if plan.proposed_action == 'change_goal' else None) == plan.next_target_id
                   and (c.entry_edge if c.target_kind == 'hotspot_route' else None) == plan.selected_entry_edge]
        if len(matches) != 1:
            raise ValueError('rule plan does not identify exactly one supplied route')
        return matches[0]

    def rule_plan(
        self,
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        candidates: Iterable[RouteCandidate] = (),
    ) -> BehaviorPlan:
        profile, state, observation, candidates = self._decision_inputs(profile, state, observation, candidates)
        if not self.supports(profile):
            self.delegated_decisions += 1
            plan = self.fallback_engine.rule_plan(profile, state, observation, candidates)
            if self.execution_contract in {'candidate-decision-v1', 'heterogeneous-learning-v2'}:
                selected = self._candidate_for_plan(plan, candidates)
                plan = replace(plan, candidate_id=selected.candidate_id if selected else None)
            return plan
        emotion, desire, choice = self._evaluate(profile, state, observation, candidates)
        if self.execution_contract is not None:
            _, _, profile, state, observation = deepcopy(self._boundary_contexts[profile.person_id][1])
        source = "bounded_rational" if self.role_configs[profile.crowd_role].capabilities.get("game") else "modeling_rule"
        plan = self._choice_to_plan(profile, observation, candidates, choice, source=source)
        self._remember(profile, observation, emotion, desire, plan)
        self.pipeline_decisions += 1
        return plan

    def set_candidate_scores(self, person_id: str, scores: dict[str, dict[str, Any]]) -> None:
        self._candidate_scores[person_id] = deepcopy(scores or {})

    async def decide(
        self,
        profile: AgentProfile,
        state: AgentState,
        observation: Observation,
        candidates: Iterable[RouteCandidate] = (),
        *, is_current=None, candidate_scores: dict[str, dict[str, Any]] | None = None,
    ) -> BehaviorPlan:
        external_current = is_current
        profile, state, observation, candidates = self._decision_inputs(profile, state, observation, candidates)
        if candidate_scores is not None:
            self.set_candidate_scores(profile.person_id, candidate_scores)
        else:
            candidate_scores = self._candidate_scores.get(profile.person_id)
        config = self.role_configs.get(profile.crowd_role)
        if config and config.methods.get("decision") != "LLM-driven":
            return self.rule_plan(profile, state, observation, candidates)
        if not self.supports(profile):
            self.delegated_decisions += 1
            return await self.fallback_engine.decide(profile, state, observation, candidates)
        emotion, desire, choice = self._evaluate(profile, state, observation, candidates)
        assembly = self.assemblies.get(profile.crowd_role)
        decision = assembly.decision if assembly is not None else self.llm
        if assembly is not None:
            _, _, profile, state, observation = deepcopy(self._boundary_contexts[profile.person_id][1])
            # Provider configuration still comes from the existing per-run binding.
            # Counters/circuit breakers remain private to the role instance.
            for key in ('base_url', 'model', 'api_key', 'timeout_seconds', 'cooldown_seconds', 'extra_body'):
                setattr(decision, key, deepcopy(getattr(self.llm, key)))
        generation = self._generation
        def is_current():
            boundary = self._boundary_contexts.get(profile.person_id)
            return generation == self._generation and (external_current is None or external_current()) and (assembly is None or boundary and boundary[0] == (observation.snapshot_id, observation.time_seconds))
        plan = await decision.decide(profile, state, observation, candidates, emotion, desire, choice,
            is_current=is_current, candidate_scores=candidate_scores)
        if not is_current():
            return plan
        self._remember(profile, observation, emotion, desire, plan)
        self.pipeline_decisions += 1
        return plan

    @staticmethod
    def _affect_factors(profile, state, observation):
        """Raw boundary readings consumed by the PAD smoothing, frozen at this moment."""
        def num(value):
            return float(value) if isinstance(value, (int, float)) else None

        state_risk = getattr(state, "perceived_risk", None)
        obs_risk = getattr(observation, "perceived_risk", None)
        risk = max(state_risk, obs_risk) if isinstance(state_risk, (int, float)) and isinstance(obs_risk, (int, float)) else (
            state_risk if isinstance(state_risk, (int, float)) else obs_risk)
        speed = getattr(getattr(observation, "own_motion", None), "speed", None)
        free = getattr(profile, "free_walking_speed", None)
        mobility = getattr(profile, "mobility", None)
        speed_ratio = None
        if isinstance(speed, (int, float)) and isinstance(free, (int, float)) and isinstance(mobility, (int, float)) and free * mobility > 0:
            speed_ratio = speed / (free * mobility)
        blocked = getattr(state, "blocked_duration", None)
        return {
            "risk": num(risk),
            "crowding": num(getattr(observation, "perceived_crowding", None)),
            "stress": num(getattr(state, "stress", None)),
            "fatigue": num(getattr(state, "fatigue", None)),
            "blockage": blocked / 30.0 if isinstance(blocked, (int, float)) else None,
            "speed_ratio": num(speed_ratio),
        }

    def _prepare_affect(self, profile, state, observation):
        config = self.role_configs[profile.crowd_role]
        assembly = self.assemblies.get(profile.crowd_role)
        context_key = (observation.snapshot_id, observation.time_seconds)
        cached = self._boundary_contexts.get(profile.person_id) if assembly is not None else None
        if cached and cached[0] == context_key:
            emotion, desire, profile, state, observation = deepcopy(cached[1])
        else:
            if assembly is not None:
                profile, state, observation = deepcopy((profile, state, observation))
            emotion_enabled = bool(config.capabilities.get("emotion"))
            factors = self._affect_factors(profile, state, observation)
            # The prior PAD is the state carried into this moment's smoothing;
            # None on the first boundary. The smoothing formula itself is untouched.
            previous_pad = self.emotional_states.get(profile.person_id)
            emotion = PADState()
            observed_emotion = None
            if emotion_enabled:
                model = assembly.emotion if assembly is not None else self.emotion
                observed_emotion = model.update(profile, state, observation, previous_pad)
                self.emotional_states[profile.person_id] = observed_emotion
                emotion = observed_emotion if assembly is None or assembly.emotion_participates else PADState()
            if config.desire_enabled:
                model = assembly.desire if assembly is not None else self.desire
                desire = model.generate(emotion, profile, state, observation)
            else:
                # No selected desire module: retain ordinary goal-oriented inputs.
                scores = {"safety": max(state.perceived_risk, observation.perceived_risk),
                          "avoid_crowding": observation.perceived_crowding, "goal_progress": 1.0 - observation.perceived_risk,
                          "group_cohesion": profile.group_cohesion, "follow_guidance": profile.authority_compliance,
                          "rest": state.fatigue}
                desire = DesireState(scores, max(scores, key=scores.get))
            continuity = ("inertial_update" if previous_pad is not None else "first_boundary") if emotion_enabled else "disabled"
            self._affect_contexts[profile.person_id] = {
                "key": context_key,
                "before_pad": previous_pad.as_dict() if previous_pad is not None else None,
                "after_pad": emotion.as_dict(),
                "continuity": continuity,
                "input_factors": factors,
                "emotion_enabled": emotion_enabled,
            }
            if assembly is not None:
                self._boundary_contexts[profile.person_id] = (context_key, deepcopy((emotion, desire, profile, state, observation)))
        return emotion, desire, profile, state, observation

    def prepare_goal_intent(self, profile, state, observation, targets=(), *, locked=False, allow_local_actions=False):
        config = self.role_configs.get(profile.crowd_role)
        if self.execution_contract not in {'desire-goal-v1', 'candidate-decision-v1', 'heterogeneous-learning-v2'} or not config or not config.desire_enabled:
            return None
        emotion, desire, frozen_profile, frozen_state, frozen_observation = self._prepare_affect(profile, state, observation)
        key = (observation.snapshot_id, observation.time_seconds)
        cached = self._goal_contexts.get(profile.person_id)
        if cached and cached['key'] == key:
            return deepcopy(cached['intent'])
        local_allowed = (allow_local_actions and frozen_observation.own_motion.stage_type == tc.STAGE_WALKING
                         and frozen_state.activity_state not in {'hotspot_dwelling', 'hotspot_departing'})
        intent = goal_intent(desire, frozen_state, deepcopy(tuple(targets)), locked=locked,
                             allow_local_actions=local_allowed)
        self._goal_contexts[profile.person_id] = {'key': key, 'intent': intent, 'candidates': None,
                                                'route_failures': {}, 'selected_goal': None}
        return deepcopy(intent)

    def finalize_goal_context(self, profile, state, observation, candidates, failures=None):
        context = self._goal_contexts.get(profile.person_id)
        if not context or context['key'] != (observation.snapshot_id, observation.time_seconds):
            return tuple(candidates)
        if context['candidates'] is None:
            context['candidates'] = deepcopy(tuple(candidates))
            context['route_failures'] = deepcopy(failures or {})
            context['selected_goal'] = state.current_goal
            # Goal preparation is the sole permitted addition to the frozen affect boundary.
            emotion, desire, fp, fs, fo = deepcopy(self._boundary_contexts[profile.person_id][1])
            fs = replace(fs, current_goal=state.current_goal, pending_goal=state.pending_goal,
                         poi_plan_active=state.poi_plan_active, activity_plan=list(state.activity_plan))
            fo = replace(fo, available_goal_ids=tuple(c.target_id for c in context['candidates']))
            self._boundary_contexts[profile.person_id] = (context['key'], (emotion, desire, fp, fs, fo))
        return deepcopy(context['candidates'])

    def prepared_goal_candidates(self, profile, observation):
        context = self._goal_contexts.get(profile.person_id)
        if context and context['key'] == (observation.snapshot_id, observation.time_seconds) and context['candidates'] is not None:
            return deepcopy(context['candidates'])
        return None

    def goal_trace(self, person_id):
        context = self._goal_contexts.get(person_id)
        if not context:
            return None
        return {'snapshot_id': context['key'][0], 'time_seconds': context['key'][1],
                **context['intent'].as_dict(), 'selected_goal': context['selected_goal'],
                'reachable_goal_ids': [candidate.target_id for candidate in context['candidates'] or ()],
                'route_failures': deepcopy(context['route_failures'])}

    def _frozen_candidates(self, profile, observation, candidates):
        context = self._goal_contexts.get(profile.person_id)
        if context and context['key'] == (observation.snapshot_id, observation.time_seconds) and context['candidates'] is not None:
            return deepcopy(context['candidates'])
        return tuple(candidates)

    def _evaluate(self, profile, state, observation, candidates):
        config = self.role_configs[profile.crowd_role]
        assembly = self.assemblies.get(profile.crowd_role)
        emotion, desire, profile, state, observation = self._prepare_affect(profile, state, observation)
        replay_values = self.replay.action_values(role_id=profile.crowd_role) if self._replay_enabled(profile) else {}
        action_bias = {action: max(-0.2, min(0.2, reward * 0.1)) for action, reward in replay_values.items()}
        game_options = None
        if config.capabilities.get("game"):
            game = assembly.game if assembly is not None else self.game
            game_options = game.evaluate_options(profile, state, observation, emotion, desire, candidates, action_bias)
            choice = game.choose(profile, state, observation, emotion, desire, candidates, action_bias)
        else:
            adjusted = state
            if config.desire_enabled:
                adjusted = replace(state, perceived_risk=max(state.perceived_risk, desire.scores["safety"]))
            base = self.fallback_engine.rule_plan(profile, adjusted, observation, candidates)
            action, target, reason = base.proposed_action, base.target_id, base.reason
            selected = self._candidate_for_plan(base, candidates) if self.execution_contract in {'candidate-decision-v1', 'heterogeneous-learning-v2'} else None
            goal = self._goal_contexts.get(profile.person_id)
            intent = goal['intent'] if goal and goal['key'] == (observation.snapshot_id, observation.time_seconds) else None
            # Goal locks never suppress explicitly allowed local affect actions.
            # Fixed itineraries/activity holds have no such permission.
            if intent and intent.local_action:
                action, target, selected, reason = intent.local_action, None, None, intent.reason
            if intent and not intent.locked:
                if intent.local_action:
                    action, target, reason = intent.local_action, None, intent.reason
                elif intent.primary in {'safety', 'group_cohesion', 'follow_guidance'} and candidates:
                    selected = candidates[0]
                    action = 'change_goal' if selected.target_kind == 'activity' else 'reroute'
                    target, reason = selected.target_id, intent.reason
            if intent and candidates and intent.primary == 'goal_progress' and not intent.locked and not intent.local_action:
                selected = next((c for c in candidates if c.target_id == state.current_goal), None)
                if selected is not None and selected.target_kind != 'activity' and action == 'continue' and state.current_plan is None:
                    action, target, reason = 'reroute', selected.target_id, intent.reason
            if config.desire_enabled and not base.route_edges and intent is None:
                if desire.primary == "rest":
                    action, target, reason = "wait", None, "emotional desire prioritizes rest"
                elif desire.primary == "avoid_crowding":
                    action, target, reason = "slow_down", None, "emotional desire prioritizes crowd avoidance"
            # Replay can prefer the safer previously rewarding alternative, not invent routes.
            if action_bias and not base.route_edges:
                options = {action: 0.1 + action_bias.get(action, 0.0)}
                options.update({item: action_bias[item] for item in ("continue", "slow_down", "wait") if item in action_bias and item != action})
                action = max(options, key=options.get)
            choice = GameChoice(action, target, desire.scores[desire.primary], {action: 1.0}, reason,
                                selected.candidate_id if selected and action in {'reroute', 'change_goal'} else None)
        self._remember_choice(profile, observation, choice, game_options, action_bias,
                              bool(config.capabilities.get("game")))
        return emotion, desire, choice

    def _remember_choice(self, profile, observation, choice, options, action_bias, game_capable):
        """Freeze the actual sampled choice, its boundary option table and replay bias.

        The choice is the production bounded-rational draw (or the confirmed rule
        fallback); it is not a post-hoc ranking of the retained table.
        """
        record = {
            "key": (observation.snapshot_id, observation.time_seconds),
            "choice": {
                "action": choice.action,
                "target_id": choice.target_id,
                "utility": choice.utility,
                "probabilities": deepcopy(choice.probabilities),
                "reason": choice.reason,
                "candidate_id": choice.candidate_id,
            },
            "options": deepcopy(options["options"]) if options is not None else None,
            "rationality": options.get("rationality") if options is not None else None,
            "selection": options.get("selection") if options is not None else "rule_fallback",
            "action_bias": deepcopy(dict(action_bias)),
            "game_capable": game_capable,
        }
        self._choice_contexts[profile.person_id] = record

    @staticmethod
    def _choice_to_plan(profile, observation, candidates, choice: GameChoice, *, source: str) -> BehaviorPlan:
        candidate = select_candidate(candidates, candidate_id=choice.candidate_id, target_id=choice.target_id) if choice.action in {'reroute', 'change_goal'} else None
        action = choice.action if choice.action not in {"reroute", "change_goal"} or candidate is not None else "continue"
        return LLMDrivenDecisionModel._make_plan(
            profile, observation, action, candidate,
            reason=choice.reason,
            confidence=None,
            source=source,
        )

    def _replay_enabled(self, profile: AgentProfile) -> bool:
        config = self.role_configs.get(profile.crowd_role)
        return bool(config and config.replay_enabled and self.execution_contract is None)

    def _remember(self, profile, observation, emotion, desire, plan) -> None:
        if not self._replay_enabled(profile):
            return
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
        active_llms = [a.decision for a in self.assemblies.values() if a.decision is not None] if self.execution_contract else [self.llm]
        llm_successes = sum(llm.successes for llm in active_llms)
        llm_error = next((llm.last_error for llm in active_llms if llm.last_error), '')
        return {
            "enabled": self.enabled,
            "llm_enabled": self.enabled,
            "pipeline_enabled": any(config.enabled for config in self.role_configs.values()),
            "model": self.model,
            "last_error": str(fallback.get("last_error") or llm_error),
            "llm_decisions": int(fallback.get("llm_decisions", 0)) + llm_successes,
            "backend": "modeling_algorithm.individual_model.pipeline",
            "implemented_chain": dict(IMPLEMENTED_CHAIN),
            "configured_roles": {
                role_id: {
                    "enabled": config.enabled,
                    "replay_enabled": config.replay_enabled and self.execution_contract is None,
                    "methods": dict(config.methods),
                    "capabilities": dict(config.capabilities),
                }
                for role_id, config in self.role_configs.items()
            },
            "pipeline_decisions": self.pipeline_decisions,
            "delegated_decisions": self.delegated_decisions,
            "fallback_decisions": self.fallback_decisions,
            "experience_replay": self.replay.diagnostics(),
            "goal_contexts": {pid: self.goal_trace(pid) for pid in self._goal_contexts},
            "module_assembly": {'contract': self.execution_contract,
                                'roles': {role: assembly.diagnostics() for role, assembly in self.assemblies.items()}},
            "role_llm": {role: a.decision.diagnostics() for role, a in self.assemblies.items() if a.decision},
            "llm": self.llm.diagnostics(),
            "fallback_engine": fallback,
        }
