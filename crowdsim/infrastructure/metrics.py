"""Metrics computed only from actual SUMO snapshots and explicit geometry."""

from __future__ import annotations

from collections import Counter
from typing import Dict

from crowdsim.domain.crowdsim_models import AgentState
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.sumo_adapter import SumoStepResult


class MetricsCollector:
    def __init__(self, network: ResearchNetwork, *, blocked_speed: float = 0.2, blocked_seconds: float = 5.0) -> None:
        self.network = network
        self.blocked_speed = blocked_speed
        self.blocked_seconds = blocked_seconds
        self.previous_edge: Dict[str, str] = {}
        self.edge_crossings = Counter()

    def measure(self, step: SumoStepResult, states: Dict[str, AgentState], population: dict) -> dict:
        counts = Counter(motion.edge_id for motion in step.persons.values())
        edge_metrics = {}
        for edge_id, count in counts.items():
            edge = self.network.edges.get(edge_id)
            widths = [lane.getWidth() for lane in edge.getLanes() if lane.allows("pedestrian")] if edge else []
            area = edge.getLength() * sum(widths) if edge and widths else None
            edge_metrics[edge_id] = {"person_count": count, "area_m2": area, "density_person_per_m2": count / area if area else None}
        for person_id, motion in step.persons.items():
            old_edge = self.previous_edge.get(person_id)
            if old_edge and old_edge != motion.edge_id:
                self.edge_crossings[(old_edge, motion.edge_id)] += 1
            self.previous_edge[person_id] = motion.edge_id
        active = list(step.persons.values())
        planned_wait = sum(person.speed < self.blocked_speed and states[person.person_id].planned_wait_until is not None for person in active)
        blocked = sum(person.speed < self.blocked_speed and states[person.person_id].blocked_duration >= self.blocked_seconds and states[person.person_id].planned_wait_until is None for person in active)
        return {"time_seconds": step.time_seconds, "pedestrian_count": len(active), "vehicle_count": len(step.vehicles), "pedestrian_avg_speed_mps": sum(person.speed for person in active) / max(1, len(active)), "vehicle_avg_speed_mps": sum(item["speed"] for item in step.vehicles.values()) / max(1, len(step.vehicles)), "queue": {"planned_wait": planned_wait, "blocked_low_speed": blocked, "unknown_low_speed": sum(person.speed < self.blocked_speed for person in active) - planned_wait - blocked}, "population": population, "edges": edge_metrics, "edge_crossings": {f"{first}->{second}": count for (first, second), count in self.edge_crossings.items()}, "units": {"speed": "m/s", "density": "person/m2", "time": "s"}}
