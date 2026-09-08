"""Generate and validate pedestrian routes against the loaded SUMO network."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from traci import constants as tc

from crowdsim.domain.crowdsim_models import MotionSnapshot
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.sumo_adapter import SumoAdapter


@dataclass(frozen=True)
class RouteCandidate:
    target_id: str
    target_edge: str
    arrival_position: float
    edges: tuple[str, ...]
    estimated_cost_seconds: float
    target_kind: str = "route"
    activity_duration: float | None = None
    next_route_edges: tuple[str, ...] = ()


class RouteProvider:
    def __init__(self, network: ResearchNetwork, adapter: SumoAdapter) -> None:
        self.network = network
        self.adapter = adapter
        self.walkable_edges = network.pedestrian_edge_ids()

    def validate_edges(self, edges: Iterable[str], *, current_edge: str | None = None) -> tuple[str, ...]:
        route = tuple(edges)
        if not route:
            raise ValueError("walking route must not be empty")
        unknown = [edge for edge in route if edge not in self.network.edges]
        if unknown:
            raise ValueError(f"unknown SUMO edge(s): {unknown}")
        forbidden = [edge for edge in route if edge not in self.walkable_edges]
        if forbidden:
            raise ValueError(f"edge(s) do not allow pedestrians: {forbidden}")
        if current_edge and route[0] != current_edge:
            raise ValueError("replacement route must start on the person's current edge")
        for first, second in zip(route, route[1:]):
            first_edge = self.network.edges[first]
            second_edge = self.network.edges[second]
            # Pedestrian walks are connected by junction topology and may not
            # have vehicle-style <connection> elements in a pedestrian-only net.
            first_nodes = {first_edge.getFromNode().getID(), first_edge.getToNode().getID()}
            second_nodes = {second_edge.getFromNode().getID(), second_edge.getToNode().getID()}
            if first_nodes.isdisjoint(second_nodes):
                raise ValueError(f"disconnected pedestrian route: {first} -> {second}")
        return route

    def build_candidate(
        self,
        motion: MotionSnapshot,
        *,
        target_id: str,
        target_edge: str,
        arrival_position: float = tc.INVALID_DOUBLE_VALUE,
    ) -> RouteCandidate:
        if motion.edge_id.startswith(":"):
            raise ValueError("route replacement is deferred while the person is on an internal junction edge")
        stages = self.adapter.find_pedestrian_route(
            motion.edge_id,
            target_edge,
            depart_pos=motion.lane_position,
            arrival_pos=arrival_position,
        )
        walking = [stage for stage in stages if int(stage.type) == tc.STAGE_WALKING]
        if len(walking) != 1 or not walking[0].edges:
            raise ValueError(f"no pure walking route from {motion.edge_id} to {target_edge}")
        edges = self.validate_edges(walking[0].edges, current_edge=motion.edge_id)
        return RouteCandidate(target_id, target_edge, arrival_position, edges, float(walking[0].cost))

    def build_activity_candidate(self, motion: MotionSnapshot, target: dict, next_target: dict | None) -> RouteCandidate:
        first = self.build_candidate(motion, target_id=target["id"], target_edge=target["edge"])
        next_edges: tuple[str, ...] = ()
        total_cost = first.estimated_cost_seconds
        if next_target is not None:
            stages = self.adapter.find_pedestrian_route(target["edge"], next_target["edge"])
            walking = [stage for stage in stages if int(stage.type) == tc.STAGE_WALKING]
            if len(walking) != 1 or not walking[0].edges:
                raise ValueError(f"no route from activity {target['id']} to {next_target['id']}")
            next_edges = self.validate_edges(walking[0].edges)
            total_cost += float(walking[0].cost)
        stay = target.get("stay_seconds", [30.0, 30.0])
        duration = (float(stay[0]) + float(stay[1])) / 2.0 if target.get("kind") == "activity" else None
        return RouteCandidate(target["id"], target["edge"], first.arrival_position, first.edges, total_cost, target.get("kind", "route"), duration, next_edges)
