"""Explicit groups made only from real SUMO person IDs."""

from __future__ import annotations

from typing import Dict, Iterable

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, GroupRecord, Observation


class GroupManager:
    def __init__(self) -> None:
        self.groups: Dict[str, GroupRecord] = {}

    def register(self, record: GroupRecord, known_person_ids: Iterable[str]) -> None:
        known = set(known_person_ids)
        members = list(dict.fromkeys(record.member_ids))
        if len(members) < 2:
            raise ValueError("a group requires at least two distinct real people")
        missing = set(members) - known
        if missing:
            raise ValueError(f"unknown group member(s): {sorted(missing)}")
        self.groups[record.group_id] = GroupRecord(record.group_id, members, record.leader_id, record.rendezvous_id)

    def update_groups(self, states: Dict[str, AgentState]) -> None:
        for group_id, record in self.groups.items():
            for person_id in record.member_ids:
                if person_id not in states:
                    continue
                state = states[person_id]
                state.group_id = group_id
                state.companion_ids = [member for member in record.member_ids if member != person_id]
                state.rendezvous_id = record.rendezvous_id

    def visible_companions(self, person_id: str, observation: Observation) -> tuple[str, ...]:
        state_group = next((record for record in self.groups.values() if person_id in record.member_ids), None)
        if state_group is None:
            return ()
        return tuple(sorted(set(state_group.member_ids) & set(observation.neighbour_ids)))

    def coordination_candidate(self, person_id: str, profile: AgentProfile, observation: Observation) -> str | None:
        visible = self.visible_companions(person_id, observation)
        if visible and profile.following_tendency * profile.group_cohesion >= 0.25:
            return "follow_group"
        return None
