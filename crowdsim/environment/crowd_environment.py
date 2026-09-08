"""Read-only local observations built from one frozen SUMO snapshot."""

from __future__ import annotations

import math
from typing import Dict, Iterable

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot, Observation
from crowdsim.infrastructure.network_adapter import ResearchNetwork


DENSITY_LEVELS = ("free", "busy", "crowded", "critical", "unknown")


class CrowdEnvironment:
    def __init__(self, network: ResearchNetwork) -> None:
        self.network = network

    def observe(
        self,
        motions: Dict[str, MotionSnapshot],
        profiles: Dict[str, AgentProfile],
        states: Dict[str, AgentState],
        snapshot_id: str,
        hazards=(),
    ) -> Dict[str, Observation]:
        by_edge: Dict[str, list[MotionSnapshot]] = {}
        for motion in motions.values():
            by_edge.setdefault(motion.edge_id, []).append(motion)
        result: Dict[str, Observation] = {}
        for person_id, motion in motions.items():
            profile = profiles[person_id]
            radius = profile.perception_radius
            neighbours = tuple(sorted(other.person_id for other in by_edge.get(motion.edge_id, ()) if other.person_id != person_id and math.hypot(other.x - motion.x, other.y - motion.y) <= radius))
            area = self._observation_area(motion.edge_id, radius)
            density = None if area is None else (len(neighbours) + 1) / area
            perceived = 0.0 if density is None else min(1.0, density / (0.75 + 2.25 * profile.crowding_tolerance))
            perceived_risk = 0.0
            for hazard in hazards:
                distance = math.hypot(motion.x - hazard.x, motion.y - hazard.y)
                if distance <= hazard.radius:
                    perceived_risk = max(perceived_risk, hazard.intensity * (1.0 - distance / max(0.01, hazard.radius)))
            state = states.get(person_id)
            result[person_id] = Observation(person_id=person_id, snapshot_id=snapshot_id, time_seconds=motion.time_seconds, own_motion=motion, neighbour_ids=neighbours, local_people_count=len(neighbours) + 1, local_area_m2=area, objective_density_per_m2=density, perceived_crowding=perceived, perceived_risk=perceived_risk, known_event_ids=tuple(sorted(state.known_events)) if state else ())
        return result

    def _observation_area(self, edge_id: str, radius: float) -> float | None:
        edge = self.network.edges.get(edge_id)
        if edge is None:
            return None
        widths = [lane.getWidth() for lane in edge.getLanes() if lane.allows("pedestrian")]
        if not widths or any(width <= 0 for width in widths):
            return None
        return max(0.01, 2.0 * radius * sum(widths))

    @staticmethod
    def classify_density(density: float | None) -> str:
        if density is None:
            return "unknown"
        if density >= 3.5:
            return "critical"
        if density >= 1.5:
            return "crowded"
        if density >= 0.5:
            return "busy"
        return "free"

    @staticmethod
    def density_counts(observations: Iterable[Observation]) -> Dict[str, int]:
        counts = {level: 0 for level in DENSITY_LEVELS}
        for observation in observations:
            counts[CrowdEnvironment.classify_density(observation.objective_density_per_m2)] += 1
        return counts
