"""Fair decision scheduling with explicit LLM budget and rule fallback."""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import replace
from typing import Dict, Iterable

from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, BehaviorPlan, Observation
from crowdsim.decision.route_provider import RouteCandidate


class DecisionScheduler:
    def __init__(self, engine: AgentDecisionEngine, *, period_seconds: float = 2.0, llm_budget: int = 12, concurrency: int = 4) -> None:
        self.engine = engine
        self.period_seconds = period_seconds
        self.llm_budget = max(0, llm_budget)
        self.concurrency = max(1, concurrency)
        self.rotation = deque()
        self.rule_count = 0
        # llm_requests: budget attempts; llm_count: successful LLM plans;
        # llm_fallbacks: attempts that degraded to the rule/game fallback.
        self.llm_requests = 0
        self.llm_count = 0
        self.llm_fallbacks = 0
        self.budget_fallback_count = 0
        self._generation = 0
        self.discarded_batches = 0
        self._llm_rotation = deque()
        self.last_sources = {}

    def invalidate(self):
        self._generation += 1
        invalidate = getattr(self.engine, 'invalidate_decisions', None)
        if invalidate:
            invalidate()

    def collect_due(self, states: Dict[str, AgentState], observations: Dict[str, Observation], now: float) -> list[str]:
        due = [person_id for person_id in observations if states[person_id].next_decision_time <= now]
        rank = {person_id: index for index, person_id in enumerate(self.rotation)}
        due.sort(key=lambda person_id: (rank.get(person_id, len(rank)), states[person_id].next_decision_time, person_id))
        return due

    def resolve_rule(self, person_ids: Iterable[str], profiles: Dict[str, AgentProfile], states: Dict[str, AgentState], observations: Dict[str, Observation], candidates: Dict[str, tuple[RouteCandidate, ...]] | None = None) -> list[BehaviorPlan]:
        person_ids = list(person_ids)
        candidates = candidates or {}
        plans = [self.engine.rule_plan(profiles[person_id], states[person_id], observations[person_id], candidates.get(person_id, ())) for person_id in person_ids]
        self.rule_count += len(plans)
        self._mark_scheduled(person_ids, states, observations)
        self.last_sources = {p: 'configured_rule' for p in person_ids}
        return [replace(plan, scheduling_source='configured_rule') for plan in plans]

    async def resolve(self, person_ids: Iterable[str], profiles: Dict[str, AgentProfile], states: Dict[str, AgentState], observations: Dict[str, Observation], *, use_llm: bool, candidates: Dict[str, tuple[RouteCandidate, ...]] | None = None, candidate_scores: Dict[str, dict] | None = None, is_current=None) -> list[BehaviorPlan]:
        person_ids = list(person_ids)
        candidates = candidates or {}
        candidate_scores = candidate_scores or {}
        generation, engine = self._generation, self.engine
        frozen_profiles, frozen_states, frozen_observations, frozen_candidates = deepcopy((
            {p: profiles[p] for p in person_ids}, {p: states[p] for p in person_ids},
            {p: observations[p] for p in person_ids}, {p: candidates.get(p, ()) for p in person_ids}))
        def current():
            return (generation == self._generation and engine is self.engine and (is_current is None or is_current())
                    and all(p in profiles and profiles[p] == frozen_profiles[p]
                            and p in observations and p in states and observations[p] == frozen_observations[p]
                            and states[p] == frozen_states[p]
                            and candidates.get(p, ()) == frozen_candidates[p] for p in person_ids))
        predicate = getattr(engine, 'llm_eligible', None)
        # Only the v2 cooperative role assembly may be admitted by its own
        # connector gate; every other path keeps the global llm mode as the gate.
        role_coop = predicate is not None and getattr(engine, 'execution_contract', None) == 'heterogeneous-learning-v2'
        def _eligible(p: str) -> bool:
            if not engine.enabled:
                return False
            if predicate is not None:
                if not role_coop and not use_llm:
                    return False
                return predicate(frozen_profiles[p], frozen_states[p], frozen_observations[p], frozen_candidates[p])
            # Legacy engine: global llm mode remains the admission gate.
            return use_llm
        eligible = [p for p in person_ids if _eligible(p)]
        rank = {p: i for i, p in enumerate(self._llm_rotation)}
        eligible.sort(key=lambda p: (rank.get(p, -1), person_ids.index(p)))
        selected = eligible[:self.llm_budget]
        fallback_ids = [p for p in person_ids if p not in selected]
        semaphore = asyncio.Semaphore(self.concurrency)
        async def decide(person_id):
            async with semaphore:
                if not current():
                    return None
                kwargs = {'is_current': current} if hasattr(engine, 'invalidate_decisions') else {}
                if hasattr(engine, 'set_candidate_scores'):
                    kwargs['candidate_scores'] = candidate_scores.get(person_id)
                return await engine.decide(frozen_profiles[person_id], frozen_states[person_id], frozen_observations[person_id], frozen_candidates[person_id], **kwargs)
        try:
            selected_plans = await asyncio.gather(*(decide(p) for p in selected))
        except asyncio.CancelledError:
            self.discarded_batches += 1
            raise
        if not current():
            self.discarded_batches += 1
            return []
        by_person = {p: plan for p, plan in zip(selected, selected_plans) if plan is not None}
        for p in fallback_ids:
            by_person[p] = engine.rule_plan(frozen_profiles[p], frozen_states[p], frozen_observations[p], frozen_candidates[p])
        success_sources = {'llm', 'llm_driven'}
        fallback_source = 'bounded_rational_fallback'
        selected_success = [p for p in selected if by_person.get(p) is not None and by_person[p].source in success_sources]
        selected_fallback = [p for p in selected if by_person.get(p) is not None and by_person[p].source == fallback_source]
        # Budget attempts are recorded separately from successful LLM plans; a degraded
        # fallback must not count as an LLM success or be labeled an LLM decision source.
        self.llm_requests += len(selected)
        self.llm_count += len(selected_success)
        self.llm_fallbacks += len(selected_fallback)
        self.rule_count += len(fallback_ids) + len(selected_fallback)
        self.budget_fallback_count += len(eligible) - len(selected)

        eligible_set = set(eligible)

        def _label(person_id: str) -> str:
            plan = by_person[person_id]
            if plan.source in success_sources:
                return 'llm_request'
            if plan.source == fallback_source:
                # Internal degradation after an LLM attempt: keep the fallback source,
                # do not relabel it as an LLM decision.
                return fallback_source
            if person_id in eligible_set:
                # Eligible but shed by the LLM budget: rule fallback with a budget label.
                return 'budget_fallback'
            # Rule-owned baseline: the scheduling affiliation is the configured rule
            # path. The plan's internal rule/game source stays on plan.source; it must
            # not replace the tracked scheduling affiliation a learning controller gates on.
            return 'configured_rule'

        self.last_sources = {p: _label(p) for p in person_ids}
        self._mark_scheduled(person_ids, states, observations)
        active = set(observations)
        self.rotation = deque(p for p in self.rotation if p in active)
        self._llm_rotation = deque(p for p in self._llm_rotation if p in active)
        for p in selected:
            if p in self._llm_rotation:
                self._llm_rotation.remove(p)
            self._llm_rotation.append(p)
        return [replace(by_person[p], scheduling_source=self.last_sources[p]) for p in person_ids]

    def _mark_scheduled(self, person_ids: Iterable[str], states: Dict[str, AgentState], observations: Dict[str, Observation]) -> None:
        for person_id in person_ids:
            states[person_id].next_decision_time = observations[person_id].time_seconds + self.period_seconds
            if person_id in self.rotation:
                self.rotation.remove(person_id)
            self.rotation.append(person_id)

    def diagnostics(self) -> dict:
        return {"rule_decisions": self.rule_count, "llm_requests": self.llm_requests,
                "llm_decisions": self.llm_count, "llm_fallbacks": self.llm_fallbacks,
                "budget_fallbacks": self.budget_fallback_count,
                "discarded_batches": self.discarded_batches, "last_scheduling_sources": dict(self.last_sources),
                **self.engine.diagnostics()}
