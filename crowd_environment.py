import math
from typing import Dict, Iterable, List

from crowdsim_models import Agent, CrowdEvent, Segment
from network_adapter import point_at_distance


DENSITY_LEVELS = ("free", "busy", "crowded", "critical")


class CrowdEnvironment:
    """Computes event pressure and each pedestrian's local perception state."""

    def __init__(self, segments: Dict[str, Segment]) -> None:
        self.segments = segments

    def event_influence(self, segment: Segment, events: Iterable[CrowdEvent]) -> float:
        influence = 0.0
        samples = self._samples(segment)
        for event in events:
            influence = max(influence, self._local_event_influence(samples, event))
        return influence

    def event_density_multiplier(self, segment: Segment, events: Iterable[CrowdEvent]) -> float:
        multiplier = 1.0
        samples = self._samples(segment)
        for event in events:
            local = self._local_event_influence(samples, event)
            multiplier = max(multiplier, 1.0 + (event.density_multiplier - 1.0) * local)
        return multiplier

    def event_effects(self, segment: Segment, events: Iterable[CrowdEvent]):
        effects = {"impact": 0.0, "speed": 0.0, "stress": 0.0, "avoidance": 0.0}
        samples = self._samples(segment)
        for event in events:
            local = self._local_event_influence(samples, event)
            effects["impact"] = max(effects["impact"], local)
            effects["speed"] = max(effects["speed"], local * event.speed_impact)
            effects["stress"] = max(effects["stress"], local * event.stress_impact)
            effects["avoidance"] = max(effects["avoidance"], local * event.avoidance_pressure)
        return effects

    def update_perception(self, agents: Iterable[Agent], events: Iterable[CrowdEvent]) -> None:
        by_edge: Dict[str, List[Agent]] = {}
        for agent in agents:
            if agent.kind == "pedestrian" and agent.route_index < len(agent.route):
                by_edge.setdefault(agent.route[agent.route_index], []).append(agent)

        active_events = list(events)
        for edge_id, local_agents in by_edge.items():
            segment = self.segments.get(edge_id)
            if segment is None:
                continue
            local_agents.sort(key=lambda item: item.distance)
            event_effects = self.event_effects(segment, active_events)
            event_pressure = event_effects["impact"]
            for agent in local_agents:
                nearby = sum(
                    other.group_size
                    for other in local_agents
                    if other.id != agent.id
                    and abs(other.distance - agent.distance) <= agent.perception_radius
                )
                area = max(1.0, 2.0 * agent.perception_radius * max(1.0, segment.width))
                agent.nearby_people = nearby
                agent.event_impact = event_pressure
                agent.event_avoidance = event_effects["avoidance"]
                agent.event_phase = "emergency" if event_pressure > 0.0 else "gathering"
                agent.local_density = (nearby + agent.group_size) / area * (1.0 + event_pressure * 2.0)
                agent.density_level = self.classify_density(
                    nearby, agent.acceptable_people, agent.local_density, agent.density_tolerance
                )
                ratio = nearby / max(1, agent.acceptable_people)
                neighbour_panic = sum(other.stress for other in local_agents if other.id != agent.id) / max(1, len(local_agents) - 1)
                pressure = min(
                    1.0,
                    ratio * 0.45
                    + agent.local_density / 5.0
                    + event_effects["stress"] * 0.5
                    + agent.flood_impact * 0.4
                    + neighbour_panic * agent.panic_susceptibility * 0.35,
                )
                resilience = 0.25 * agent.risk_tolerance + 0.2 * agent.familiarity
                agent.stress = max(
                    0.0,
                    min(1.0, agent.stress * 0.78 + pressure * 0.32 - resilience * 0.08),
                )

    @staticmethod
    def classify_density(nearby: int, acceptable: int, density: float, tolerance: float = 7.0) -> str:
        ratio = nearby / max(1, acceptable)
        if ratio >= 2.0 or density >= tolerance:
            return "critical"
        if ratio >= 1.0 or density >= max(5.0, tolerance * 0.75):
            return "crowded"
        if ratio >= 0.55 or density >= 2.0:
            return "busy"
        return "free"

    @staticmethod
    def density_counts(agents: Iterable[Agent]) -> Dict[str, int]:
        pedestrians = [agent for agent in agents if agent.kind == "pedestrian"]
        return {
            level: sum(agent.density_level == level for agent in pedestrians)
            for level in DENSITY_LEVELS
        }

    @staticmethod
    def _samples(segment: Segment):
        return [point_at_distance(segment.points, segment.length * ratio)[:2] for ratio in (0.2, 0.5, 0.8)]

    @staticmethod
    def _local_event_influence(samples, event: CrowdEvent) -> float:
        local = 0.0
        for x, y in samples:
            distance = math.hypot(x - event.x, y - event.y)
            if distance <= event.radius:
                local = max(local, event.intensity * (1.0 - distance / event.radius))
        return local
