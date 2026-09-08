"""Deterministic POI knowledge and activity-plan initialisation."""

from __future__ import annotations

import hashlib

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState
from crowdsim.environment.poi_catalog import PoiCatalog


class ActivityPlanner:
    def __init__(self, catalog: PoiCatalog) -> None:
        self.catalog = catalog

    def initialize(self, profile: AgentProfile, state: AgentState) -> None:
        if state.activity_plan:
            return
        exits = sorted(item["id"] for item in self.catalog.pois.values() if item["kind"] == "exit")
        activities = sorted(item["id"] for item in self.catalog.pois.values() if item["kind"] == "activity")
        ranked = sorted(activities, key=lambda poi_id: self._rank(profile.person_id, poi_id))
        known_count = min(len(ranked), max(0, round(profile.familiarity * len(ranked))))
        known_activities = ranked[:known_count]
        if profile.visit_purpose in {"tourism", "leisure"} and known_activities:
            count = len(known_activities) if profile.visit_purpose == "tourism" else 1
            state.activity_plan = [*known_activities[:count], *exits[:1]]
        else:
            state.activity_plan = exits[:1]
        state.current_goal = state.activity_plan[0] if state.activity_plan else None

    def available_ids(self, state: AgentState, now: float) -> tuple[str, ...]:
        planned = set(state.activity_plan)
        return tuple(item["id"] for item in self.catalog.available(now, planned))

    @staticmethod
    def _rank(person_id: str, poi_id: str) -> bytes:
        return hashlib.sha256(f"{person_id}:{poi_id}".encode("utf-8")).digest()
