"""Fair decision scheduling with explicit LLM budget and rule fallback."""

from __future__ import annotations

import asyncio
from collections import deque
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
        self.llm_count = 0
        self.budget_fallback_count = 0

    def collect_due(self, states: Dict[str, AgentState], observations: Dict[str, Observation], now: float) -> list[str]:
        due = [person_id for person_id in observations if states[person_id].next_decision_time <= now]
        rank = {person_id: index for index, person_id in enumerate(self.rotation)}
        due.sort(key=lambda person_id: (rank.get(person_id, len(rank)), states[person_id].next_decision_time, person_id))
        return due

    def resolve_rule(self, person_ids: Iterable[str], profiles: Dict[str, AgentProfile], states: Dict[str, AgentState], observations: Dict[str, Observation], candidates: Dict[str, tuple[RouteCandidate, ...]] | None = None) -> list[BehaviorPlan]:
        candidates = candidates or {}
        plans = [self.engine.rule_plan(profiles[person_id], states[person_id], observations[person_id], candidates.get(person_id, ())) for person_id in person_ids]
        self.rule_count += len(plans)
        self._mark_scheduled(person_ids, states, observations)
        return plans

    async def resolve(self, person_ids: Iterable[str], profiles: Dict[str, AgentProfile], states: Dict[str, AgentState], observations: Dict[str, Observation], *, use_llm: bool, candidates: Dict[str, tuple[RouteCandidate, ...]] | None = None) -> list[BehaviorPlan]:
        person_ids = list(person_ids)
        candidates = candidates or {}
        if not use_llm or not self.engine.enabled:
            return self.resolve_rule(person_ids, profiles, states, observations, candidates)
        selected, fallback_ids = person_ids[: self.llm_budget], person_ids[self.llm_budget :]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def decide(person_id: str):
            async with semaphore:
                return await self.engine.decide(profiles[person_id], states[person_id], observations[person_id], candidates.get(person_id, ()))

        plans = list(await asyncio.gather(*(decide(person_id) for person_id in selected)))
        self.llm_count += sum(plan.source == "llm" for plan in plans)
        if fallback_ids:
            plans.extend(self.resolve_rule(fallback_ids, profiles, states, observations, candidates))
            self.budget_fallback_count += len(fallback_ids)
        self._mark_scheduled(selected, states, observations)
        return plans

    def _mark_scheduled(self, person_ids: Iterable[str], states: Dict[str, AgentState], observations: Dict[str, Observation]) -> None:
        for person_id in person_ids:
            states[person_id].next_decision_time = observations[person_id].time_seconds + self.period_seconds
            if person_id in self.rotation:
                self.rotation.remove(person_id)
            self.rotation.append(person_id)

    def diagnostics(self) -> dict:
        return {"rule_decisions": self.rule_count, "llm_decisions": self.llm_count, "budget_fallbacks": self.budget_fallback_count, **self.engine.diagnostics()}
