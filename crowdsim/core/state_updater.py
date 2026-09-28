"""Synchronous psychological and fatigue state update."""

from __future__ import annotations

from dataclasses import replace
from typing import Dict

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, Observation
from crowdsim.domain.crowd_visual_state import CrowdVisualPolicy


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


class StateUpdater:
    def __init__(self, visual_policy=None):
        self.visual_policy = visual_policy or CrowdVisualPolicy()

    def update_all(
        self,
        old_states: Dict[str, AgentState],
        profiles: Dict[str, AgentProfile],
        observations: Dict[str, Observation],
        dt: float,
    ) -> Dict[str, AgentState]:
        updated: Dict[str, AgentState] = {}
        for person_id, observation in observations.items():
            old = old_states.get(person_id, AgentState(person_id))
            profile = profiles[person_id]
            neighbour_stress = sum(old_states.get(other, AgentState(other)).stress for other in observation.neighbour_ids) / max(1, len(observation.neighbour_ids))
            stimulus_rate = profile.stress_susceptibility * (0.16 * observation.perceived_crowding + 0.24 * observation.perceived_risk + 0.08 * neighbour_stress)
            recovery_rate = old.stress / max(1.0, profile.recovery_seconds)
            stress = _clip(old.stress + dt * (stimulus_rate - recovery_rate))
            walking = observation.own_motion.speed > 0.1
            if walking:
                effort = observation.own_motion.speed / max(0.1, profile.free_walking_speed * profile.mobility)
                fatigue = _clip(old.fatigue + dt * 0.002 * effort * (1.25 - profile.endurance))
            else:
                fatigue = _clip(old.fatigue - dt * 0.004 * (0.5 + profile.endurance))
            planned_stop = (
                old.planned_wait_until is not None
                or old.activity_state == "hotspot_dwelling"
            )
            blocked = old.blocked_duration + dt if observation.own_motion.speed < 0.2 and not planned_stop else 0.0
            updated[person_id] = replace(old, stress=stress, fatigue=fatigue, perceived_risk=observation.perceived_risk, perceived_crowding=observation.perceived_crowding, blocked_duration=blocked, known_events=dict(old.known_events), received_messages=list(old.received_messages), activity_plan=list(old.activity_plan), companion_ids=list(old.companion_ids), **self.visual_policy.durations(old, observation, dt))
        return updated
