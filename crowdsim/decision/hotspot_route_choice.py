"""Build congestion-aware route alternatives for goal-locked hotspot visitors."""

from __future__ import annotations

from dataclasses import replace

from crowdsim.decision.route_provider import RouteCandidate, RouteProvider, RouteUnavailable
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot
from crowdsim.infrastructure.network_adapter import ResearchNetwork


class HotspotRouteChoice:
    def __init__(self, network: ResearchNetwork, route_provider: RouteProvider) -> None:
        self.network = network
        self.route_provider = route_provider
        self._cost_snapshots: dict[tuple[str, int], dict[str, float]] = {}

    def build_candidates(
        self,
        motion: MotionSnapshot,
        profile: AgentProfile,
        state: AgentState,
        hotspot: dict,
        edge_metrics: dict,
    ) -> tuple[RouteCandidate, ...]:
        policy = hotspot.get("route_choice", {})
        if not policy.get("enabled"):
            return ()
        target_edges = set(hotspot["target_edges"])
        entry_edges = tuple(hotspot["entry_edges"])
        park_edges = set(
            hotspot.get("approach_edges") or hotspot.get("park_access_edges", ())
        )
        target_position = hotspot.get("target_position")
        if target_position is None:
            raise ValueError(f"hotspot visitor route is missing target_position for {hotspot['id']}")
        if motion.edge_id in target_edges or motion.edge_id in entry_edges:
            # Entering the narrow connector commits the visitor to that entrance.
            return ()
        inside_park = motion.edge_id in park_edges
        if state.hotspot_entry_edge is not None and not inside_park:
            # Select once at departure, then reconsider only at the configured
            # park network where a physically valid alternative exists.
            return ()
        if motion.time_seconds + 1e-9 < state.hotspot_next_route_check:
            return ()
        state.hotspot_next_route_check = motion.time_seconds + float(policy["decision_interval_seconds"])

        epoch = int(
            motion.time_seconds
            // max(0.1, float(policy["decision_interval_seconds"]))
        )
        snapshot_key = (hotspot["id"], epoch)
        dynamic_costs = self._cost_snapshots.get(snapshot_key)
        if dynamic_costs is None:
            dynamic_costs = self._dynamic_edge_costs(hotspot, edge_metrics)
            self._cost_snapshots = {snapshot_key: dynamic_costs}
        cost_token = ("hotspot", *snapshot_key)
        reference_speed = float(hotspot.get("reference_walking_speed_mps", 1.2))
        if not entry_edges:
            try:
                candidate = self.route_provider.build_candidate(
                    motion,
                    target_id=hotspot["id"],
                    target_edge=hotspot["target_edge"],
                    arrival_position=target_position,
                    edge_costs=dynamic_costs,
                    cost_token=cost_token,
                    default_speed_mps=reference_speed,
                    forbidden_edges=hotspot.get("excluded_edges", ()),
                )
            except RouteUnavailable:
                return ()
            return (replace(
                candidate,
                target_kind="hotspot_route",
                minimum_savings_seconds=float(policy["minimum_savings_seconds"]),
                switch_cooldown_seconds=float(policy["switch_cooldown_seconds"]),
            ),)

        candidates = []
        for entry_edge in entry_edges:
            forbidden = (
                set(hotspot.get("excluded_edges", ()))
                | (set(entry_edges) - {entry_edge})
            )
            try:
                candidate = self.route_provider.build_via_candidate(
                    motion,
                    target_id=hotspot["id"],
                    target_edge=hotspot["target_edge"],
                    via_edge=entry_edge,
                    arrival_position=target_position,
                    edge_costs=dynamic_costs,
                    cost_token=cost_token,
                    default_speed_mps=reference_speed,
                    forbidden_edges=forbidden,
                    via_orientation=self._portal_orientation(hotspot, entry_edge),
                )
            except RouteUnavailable:
                continue
            if not self._valid_hotspot_route(candidate, entry_edge, hotspot, inside_park):
                continue
            entry_density = float(edge_metrics.get(entry_edge, {}).get("density_person_per_m2") or 0.0)
            candidates.append(replace(
                candidate,
                entry_density_person_per_m2=entry_density,
                minimum_savings_seconds=float(policy["minimum_savings_seconds"]),
                switch_cooldown_seconds=float(policy["switch_cooldown_seconds"]),
            ))
        return tuple(sorted(candidates, key=lambda item: (item.estimated_cost_seconds, item.entry_edge or "")))

    @staticmethod
    def _portal_orientation(hotspot: dict, edge_id: str) -> int | None:
        for portal in hotspot.get("access_portals", ()):
            if portal.get("edge") != edge_id:
                continue
            outside = portal.get("outside_side", "auto")
            inside = portal.get("inside_side", "auto")
            if outside in {"start", "from"}:
                return 0
            if outside in {"end", "to"}:
                return 1
            if inside in {"start", "from"}:
                return 1
            if inside in {"end", "to"}:
                return 0
        return None

    @staticmethod
    def _valid_hotspot_route(candidate: RouteCandidate, entry_edge: str, hotspot: dict, inside_park: bool) -> bool:
        edges = candidate.edges
        if entry_edge not in edges or set(edges) & set(hotspot.get("excluded_edges", ())):
            return False
        entry_index = edges.index(entry_edge)
        prefix = edges[:entry_index]
        if set(prefix) & set(hotspot["target_edges"]):
            return False
        other_entries = set(hotspot["entry_edges"]) - {entry_edge}
        if set(edges) & other_entries:
            return False
        if inside_park:
            allowed_prefix = set(
                hotspot.get("approach_edges") or hotspot.get("park_access_edges", ())
            )
            if any(not edge.startswith(":") and edge not in allowed_prefix for edge in prefix):
                return False
        return True

    def _dynamic_edge_costs(self, hotspot, edge_metrics) -> dict[str, float]:
        """Build one shared travel-time snapshot for all visitors in this cycle.

        Profiles decide whether a saving is worth a route change; they do not
        trigger a separate shortest-path tree for every person.
        """
        policy = hotspot["route_choice"]
        free_density = float(policy["free_flow_density_person_per_m2"])
        congested_density = float(policy["congested_density_person_per_m2"])
        density_span = max(0.01, congested_density - free_density)
        speed_floor = float(policy["speed_floor_mps"])
        free_speed = max(
            speed_floor,
            float(hotspot.get("reference_walking_speed_mps", 1.2)),
        )
        costs = {}
        for edge_id, metric in edge_metrics.items():
            if edge_id not in self.route_provider.edge_lengths:
                continue
            metric = edge_metrics.get(edge_id, {})
            count = int(metric.get("person_count") or 0)
            if count <= 0:
                continue
            density = float(metric.get("density_person_per_m2") or 0.0)
            raw_speed = float(metric.get("avg_speed_mps") or 0.0)
            observed_speed = max(speed_floor, raw_speed) if raw_speed > 0.0 else free_speed
            length = self.route_provider.edge_lengths[edge_id]
            free_time = length / free_speed
            observed_time = length / observed_speed
            density_time = free_time * (
                1.0 + max(0.0, density - free_density) / density_span
            )
            costs[edge_id] = max(free_time, observed_time, density_time)
        return costs
