"""Metrics computed only from actual SUMO snapshots and explicit geometry."""

from __future__ import annotations

from collections import Counter
from typing import Dict
from traci import constants as tc

from crowdsim.domain.crowdsim_models import AgentState
from crowdsim.infrastructure.hotspot_phase import HotspotPhaseTracker
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.sumo_adapter import SumoStepResult


class MetricsCollector:
    def __init__(self, network: ResearchNetwork, *, blocked_speed: float = 0.2, blocked_seconds: float = 5.0, slow_walking_speed: float = 0.5, hotspots=None, population_manager=None) -> None:
        self.network = network
        self.blocked_speed = blocked_speed
        self.blocked_seconds = blocked_seconds
        self.slow_walking_speed = slow_walking_speed
        self.previous_edge: Dict[str, str] = {}
        self.edge_crossings = Counter()
        self.hotspots = hotspots
        self.population_manager = population_manager
        self.hotspot_phase_trackers = {
            hotspot_id: HotspotPhaseTracker(hotspot.get("phase_thresholds"))
            for hotspot_id, hotspot in (hotspots.hotspots.items() if hotspots is not None else ())
        }
        self.hotspot_core_phase_trackers = {
            hotspot_id: HotspotPhaseTracker(hotspot.get("phase_thresholds"))
            for hotspot_id, hotspot in (hotspots.hotspots.items() if hotspots is not None else ())
        }

    def measure(self, step: SumoStepResult, states: Dict[str, AgentState], population: dict) -> dict:
        counts = Counter(motion.edge_id for motion in step.persons.values())
        speeds_by_edge = {}
        for motion in step.persons.values():
            speeds_by_edge.setdefault(motion.edge_id, []).append(motion.speed)
        edge_metrics = {}
        for edge_id, count in counts.items():
            edge = self.network.edges.get(edge_id)
            widths = [lane.getWidth() for lane in edge.getLanes() if lane.allows("pedestrian")] if edge else []
            area = edge.getLength() * sum(widths) if edge and widths else None
            speeds = speeds_by_edge[edge_id]
            edge_metrics[edge_id] = {
                "person_count": count,
                "area_m2": area,
                "density_person_per_m2": count / area if area else None,
                "avg_speed_mps": sum(speeds) / len(speeds),
            }
        for person_id, motion in step.persons.items():
            if motion.edge_id.startswith(":"):
                # Preserve the last normal edge so a junction-internal sample
                # does not hide a real park -> entrance crossing.
                continue
            old_edge = self.previous_edge.get(person_id)
            if old_edge and old_edge != motion.edge_id:
                self.edge_crossings[(old_edge, motion.edge_id)] += 1
            self.previous_edge[person_id] = motion.edge_id
        active = list(step.persons.values())
        def planned_stop(person_id: str) -> bool:
            state = states.get(person_id, AgentState(person_id))
            return state.planned_wait_until is not None or state.activity_state == "hotspot_dwelling"

        planned_wait = sum(
            person.speed < self.blocked_speed and planned_stop(person.person_id)
            for person in active
        )
        blocked = sum(
            person.speed < self.blocked_speed
            and states.get(person.person_id, AgentState(person.person_id)).blocked_duration >= self.blocked_seconds
            and not planned_stop(person.person_id)
            for person in active
        )
        hotspot_metrics = {}
        if self.hotspots is not None:
            for hotspot_id, hotspot in self.hotspots.hotspots.items():
                edge_ids = set(hotspot["measurement_edges"])
                members = [motion for motion in active if motion.edge_id in edge_ids]
                dwelling_ids = {
                    motion.person_id for motion in members
                    if states.get(motion.person_id, AgentState(motion.person_id)).activity_state
                    == "hotspot_dwelling"
                }
                walking = [
                    motion for motion in members
                    if motion.stage_type == tc.STAGE_WALKING and motion.person_id not in dwelling_ids
                ]
                stopped = [
                    motion for motion in members
                    if motion.stage_type == tc.STAGE_WAITING or motion.person_id in dwelling_ids
                ]
                target_edge_ids = set(hotspot["target_edges"])
                core_members = [motion for motion in members if motion.edge_id in target_edge_ids]
                core_walking = [
                    motion for motion in core_members
                    if motion.stage_type == tc.STAGE_WALKING and motion.person_id not in dwelling_ids
                ]
                # Include empty measured edges in the fixed observation area.
                total_area = 0.0
                for edge_id in edge_ids:
                    edge = self.network.edges[edge_id]
                    widths = [lane.getWidth() for lane in edge.getLanes() if lane.allows("pedestrian")]
                    total_area += edge.getLength() * sum(widths)
                core_area = 0.0
                by_target_edge = {}
                for edge_id in target_edge_ids:
                    edge = self.network.edges[edge_id]
                    edge_area = edge.getLength() * sum(
                        lane.getWidth() for lane in edge.getLanes() if lane.allows("pedestrian")
                    )
                    core_area += edge_area
                    metric = edge_metrics.get(edge_id, {
                        "person_count": 0,
                        "area_m2": edge_area,
                        "density_person_per_m2": 0.0,
                        "avg_speed_mps": 0.0,
                    })
                    by_target_edge[edge_id] = metric
                core_metric = {
                    "person_count": len(core_members),
                    "area_m2": core_area,
                    "density_person_per_m2": len(core_members) / core_area if core_area else None,
                    "walking_count": len(core_walking),
                    "stopped_count": sum(
                        motion.stage_type == tc.STAGE_WAITING or motion.person_id in dwelling_ids
                        for motion in core_members
                    ),
                    "slow_walking_count": sum(motion.speed < self.slow_walking_speed for motion in core_walking),
                    "avg_speed_mps": sum(motion.speed for motion in core_members) / max(1, len(core_members)),
                    "target_edges": sorted(target_edge_ids),
                    "by_target_edge": by_target_edge,
                }
                visit_lifecycle = self._visit_lifecycle(hotspot_id, hotspot, step, states)
                process_state = self.hotspot_phase_trackers[hotspot_id].update(
                    step.time_seconds,
                    len(members),
                    core_metric["density_person_per_m2"],
                    remaining_visitor_count=visit_lifecycle["remaining_visitor_count"],
                    blocked_person_count=visit_lifecycle["blocked_network_count"],
                )
                core_process_state = self.hotspot_core_phase_trackers[hotspot_id].update(
                    step.time_seconds,
                    len(members),
                    core_metric["density_person_per_m2"],
                )
                entry_metrics = self._zone_metrics(
                    set(hotspot.get("entry_edges", ())), active, edge_metrics
                )
                park_metrics = self._zone_metrics(
                    set(hotspot.get("park_access_edges", ())), active, edge_metrics
                )
                park_entry_metrics = self._zone_metrics(
                    set(hotspot.get("park_entry_edges", ())), active, edge_metrics
                )
                external_approach_metrics = self._zone_metrics(
                    set(hotspot.get("external_approach_edges", ())), active, edge_metrics
                )
                entry_edges = set(hotspot.get("entry_edges", ()))
                park_edges = set(hotspot.get("park_access_edges", ()))
                park_entry_edges = set(hotspot.get("park_entry_edges", ()))
                external_edges = set(hotspot.get("external_approach_edges", ()))
                inbound_crossings = sum(
                    count for (first, second), count in self.edge_crossings.items()
                    if first in park_edges and second in entry_edges
                )
                outbound_crossings = sum(
                    count for (first, second), count in self.edge_crossings.items()
                    if first in entry_edges and second in park_edges
                )
                entry_metrics.update({
                    "inbound_crossing_count": inbound_crossings,
                    "outbound_crossing_count": outbound_crossings,
                    "net_inflow_count": inbound_crossings - outbound_crossings,
                })
                park_metrics.update({
                    "queued_hotspot_visitor_count": visit_lifecycle["queued_in_park_count"],
                    "blocked_departing_visitor_count": visit_lifecycle["blocked_departing_in_park_count"],
                })
                park_inbound_crossings = sum(
                    count for (first, second), count in self.edge_crossings.items()
                    if first in external_edges and second in park_entry_edges
                )
                park_outbound_crossings = sum(
                    count for (first, second), count in self.edge_crossings.items()
                    if first in park_entry_edges and second in external_edges
                )
                park_entry_metrics.update({
                    "inbound_crossing_count": park_inbound_crossings,
                    "outbound_crossing_count": park_outbound_crossings,
                    "net_inflow_count": park_inbound_crossings - park_outbound_crossings,
                })
                external_approach_metrics.update({
                    "queued_hotspot_visitor_count": visit_lifecycle["queued_outside_park_count"],
                    "blocked_departing_visitor_count": visit_lifecycle["blocked_departing_outside_park_count"],
                })
                hotspot_metrics[hotspot_id] = {
                    "name": hotspot.get("name", hotspot_id),
                    "person_count": len(members),
                    "moving_count": sum(motion.speed >= self.blocked_speed for motion in members),
                    "low_speed_count": sum(motion.speed < self.blocked_speed for motion in members),
                    "walking_count": len(walking),
                    "stopped_count": len(stopped),
                    "slow_walking_count": sum(motion.speed < self.slow_walking_speed for motion in walking),
                    "slow_walking_threshold_mps": self.slow_walking_speed,
                    "avg_walking_speed_mps": sum(motion.speed for motion in walking) / max(1, len(walking)),
                    "avg_speed_mps": sum(motion.speed for motion in members) / max(1, len(members)),
                    "area_m2": total_area,
                    "density_person_per_m2": len(members) / total_area if total_area else None,
                    "measurement_edges": sorted(edge_ids),
                    "core": core_metric,
                    "entries": entry_metrics,
                    "park_entries": park_entry_metrics,
                    "park": park_metrics,
                    "external_approach": external_approach_metrics,
                    "visit_lifecycle": visit_lifecycle,
                    "process_state": process_state,
                    "core_process_state": core_process_state,
                }
        return {"time_seconds": step.time_seconds, "pedestrian_count": len(active), "vehicle_count": len(step.vehicles), "pedestrian_avg_speed_mps": sum(person.speed for person in active) / max(1, len(active)), "vehicle_avg_speed_mps": sum(item["speed"] for item in step.vehicles.values()) / max(1, len(step.vehicles)), "queue": {"planned_wait": planned_wait, "blocked_low_speed": blocked, "unknown_low_speed": sum(person.speed < self.blocked_speed for person in active) - planned_wait - blocked}, "population": population, "edges": edge_metrics, "hotspots": hotspot_metrics, "edge_crossings": {f"{first}->{second}": count for (first, second), count in self.edge_crossings.items()}, "units": {"speed": "m/s", "density": "person/m2", "time": "s"}}

    def _visit_lifecycle(self, hotspot_id: str, hotspot: dict, step: SumoStepResult, states) -> dict:
        manager = self.population_manager
        if manager is None:
            return {
                "planned_count": 0,
                "not_departed_count": 0,
                "approaching_count": 0,
                "queued_in_park_count": 0,
                "queued_outside_park_count": 0,
                "entering_count": 0,
                "gathering_count": 0,
                "departing_count": 0,
                "completed_count": 0,
                "arrived_at_target_count": 0,
                "active_visitor_count": 0,
                "remaining_visitor_count": 0,
                "network_backlog_count": 0,
                "blocked_departing_count": 0,
                "blocked_departing_in_park_count": 0,
                "blocked_departing_outside_park_count": 0,
                "blocked_network_count": 0,
                "completion_fraction": 0.0,
                "target_arrival_fraction": 0.0,
            }
        visitor_ids = {
            person_id for person_id, assigned_hotspot in manager.hotspot_ids.items()
            if assigned_hotspot == hotspot_id
        }
        active_ids = visitor_ids & set(step.persons)
        completed_ids = visitor_ids & manager.ledger.arrived_ids
        departed_ids = visitor_ids & manager.ledger.departed_ids
        counts = Counter()
        target_edges = set(hotspot["target_edges"])
        entry_edges = set(hotspot.get("entry_edges", ()))
        park_edges = set(hotspot.get("park_access_edges", ()))
        external_edges = set(hotspot.get("external_approach_edges", ()))
        for person_id in active_ids:
            motion = step.persons[person_id]
            state = states.get(person_id, AgentState(person_id))
            if (
                state.activity_state == "hotspot_dwelling"
                or motion.stage_type == tc.STAGE_WAITING
            ) and motion.edge_id in target_edges:
                counts["gathering"] += 1
            elif state.activity_state == "hotspot_departing" or (
                motion.stage_type == tc.STAGE_WALKING
                and motion.remaining_stage_count <= 1
                and state.activity_state != "hotspot_approaching"
            ):
                counts["departing"] += 1
                if motion.edge_id in (park_edges | entry_edges | external_edges) and (
                    motion.speed < self.slow_walking_speed
                    or state.blocked_duration >= self.blocked_seconds
                ):
                    counts["blocked_departing"] += 1
                    if motion.edge_id in external_edges:
                        counts["blocked_departing_outside_park"] += 1
                    else:
                        counts["blocked_departing_in_park"] += 1
            elif motion.edge_id in entry_edges or motion.edge_id in target_edges:
                counts["entering"] += 1
            elif motion.edge_id in park_edges and (
                motion.speed < self.slow_walking_speed
                or state.blocked_duration >= self.blocked_seconds
            ):
                counts["queued_in_park"] += 1
            elif motion.edge_id in external_edges and (
                motion.speed < self.slow_walking_speed
                or state.blocked_duration >= self.blocked_seconds
            ):
                counts["queued_outside_park"] += 1
            else:
                counts["approaching"] += 1
        arrived_at_target = counts["gathering"] + counts["departing"] + len(completed_ids)
        network_backlog = (
            counts["approaching"]
            + counts["queued_outside_park"]
            + counts["queued_in_park"]
            + counts["entering"]
            + counts["departing"]
        )
        remaining = len(visitor_ids - completed_ids)
        blocked_network = (
            counts["queued_outside_park"]
            + counts["queued_in_park"]
            + counts["blocked_departing"]
        )
        planned = len(visitor_ids)
        return {
            "planned_count": planned,
            "not_departed_count": len(visitor_ids - departed_ids),
            "approaching_count": counts["approaching"],
            "queued_in_park_count": counts["queued_in_park"],
            "queued_outside_park_count": counts["queued_outside_park"],
            "entering_count": counts["entering"],
            "gathering_count": counts["gathering"],
            "departing_count": counts["departing"],
            "completed_count": len(completed_ids),
            "arrived_at_target_count": arrived_at_target,
            "active_visitor_count": len(active_ids),
            "remaining_visitor_count": remaining,
            "network_backlog_count": network_backlog,
            "blocked_departing_count": counts["blocked_departing"],
            "blocked_departing_in_park_count": counts["blocked_departing_in_park"],
            "blocked_departing_outside_park_count": counts["blocked_departing_outside_park"],
            "blocked_network_count": blocked_network,
            "completion_fraction": len(completed_ids) / planned if planned else 0.0,
            "target_arrival_fraction": arrived_at_target / planned if planned else 0.0,
        }

    def _zone_metrics(self, edge_ids: set[str], active, edge_metrics: dict) -> dict:
        members = [motion for motion in active if motion.edge_id in edge_ids]
        walking = [motion for motion in members if motion.stage_type == tc.STAGE_WALKING]
        area = 0.0
        by_edge = {}
        for edge_id in edge_ids:
            edge = self.network.edges[edge_id]
            edge_area = edge.getLength() * sum(
                lane.getWidth() for lane in edge.getLanes() if lane.allows("pedestrian")
            )
            area += edge_area
            by_edge[edge_id] = edge_metrics.get(edge_id, {
                "person_count": 0,
                "area_m2": edge_area,
                "density_person_per_m2": 0.0,
                "avg_speed_mps": 0.0,
            })
        return {
            "person_count": len(members),
            "walking_count": len(walking),
            "low_speed_walking_count": sum(motion.speed < self.slow_walking_speed for motion in walking),
            "blocked_walking_count": sum(motion.speed < self.blocked_speed for motion in walking),
            "avg_walking_speed_mps": sum(motion.speed for motion in walking) / max(1, len(walking)),
            "area_m2": area,
            "density_person_per_m2": len(members) / area if area else None,
            "edges": sorted(edge_ids),
            "by_edge": by_edge,
        }
