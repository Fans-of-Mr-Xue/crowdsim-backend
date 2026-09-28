"""Position-aware pedestrian routing on SUMO's pedestrian connection graph.

SUMO and sumolib normally expose routes as sequences of edges.  That is not
enough for hotspot routing: two people on the same edge can be closer to
opposite endpoints, and a target in the middle of a ring can be reached from
either direction.  This module keeps the two endpoints of every pedestrian
edge distinct and only joins them through SUMO's pedestrian connections.

The expensive result cached here is a shortest-path tree rooted at an edge
endpoint.  Exact source and target positions are attached to those trees with
small partial-edge costs, so continuous positions do not destroy cache reuse.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
import heapq
import itertools
import math
from typing import Hashable, Iterable, Mapping


Endpoint = tuple[str, int]


class PositionRouteUnavailable(ValueError):
    """No legal pedestrian route exists for the requested positions."""


@dataclass(frozen=True)
class EdgePosition:
    edge_id: str
    offset_m: float


@dataclass(frozen=True)
class PositionRoute:
    edges: tuple[str, ...]
    distance_m: float
    cost: float


@dataclass(frozen=True)
class _Arc:
    to: Endpoint
    edge_id: str | None = None
    direction: int = 0


@dataclass(frozen=True)
class _Tree:
    distance: dict[Endpoint, float]
    next_step: dict[Endpoint, tuple[Endpoint, _Arc]]


class PositionAwarePedestrianRouter:
    """Route between exact positions while sharing endpoint path trees."""

    def __init__(self, network, *, cache_limit: int = 64) -> None:
        self.network = network
        self.net = getattr(network, "net", network)
        source_edges = getattr(network, "edges", None)
        if source_edges is None:
            source_edges = {
                edge.getID(): edge for edge in self.net.getEdges(withInternal=True)
            }
        self.edges = dict(source_edges)
        self.walkable_edges = {
            edge_id
            for edge_id, edge in self.edges.items()
            if any(lane.allows("pedestrian") for lane in edge.getLanes())
        }
        self.edge_lengths = {
            edge_id: self._walking_length(self.edges[edge_id])
            for edge_id in self.walkable_edges
        }
        self.adjacency: dict[Endpoint, list[_Arc]] = defaultdict(list)
        self.cache_limit = max(1, int(cache_limit))
        self._trees: OrderedDict[tuple, _Tree] = OrderedDict()
        self._dynamic_cost_token: Hashable | None = None
        self.counters = {
            "graph_nodes": 0,
            "graph_arcs": 0,
            "tree_builds": 0,
            "tree_cache_hits": 0,
            "route_queries": 0,
        }
        self._build_graph()

    @staticmethod
    def _walking_length(edge) -> float:
        lanes = [lane for lane in edge.getLanes() if lane.allows("pedestrian")]
        if not lanes:
            raise ValueError(f"edge does not allow pedestrians: {edge.getID()}")
        return min(float(edge.getLength()), *(float(lane.getLength()) for lane in lanes))

    def _build_graph(self) -> None:
        has_walkingarea = any(
            self.edges[edge_id].getFunction() == "walkingarea"
            for edge_id in self.walkable_edges
        )
        for edge_id in self.walkable_edges:
            start = (edge_id, 0)
            end = (edge_id, 1)
            self._add_arc(start, _Arc(end, edge_id, 1))
            self._add_arc(end, _Arc(start, edge_id, -1))

        connection_count = 0
        for edge_id in self.walkable_edges:
            edge = self.edges[edge_id]
            try:
                outgoing = edge.getAllowedOutgoing("pedestrian")
            except (AttributeError, TypeError):
                outgoing = ()
            for successor in outgoing:
                successor_id = successor.getID()
                if successor_id not in self.walkable_edges:
                    continue
                # A SUMO connection leaves the directed edge at its "to" side
                # and enters the successor at its "from" side.  Pedestrians
                # may walk the connection in reverse, so store both arcs.
                first = (edge_id, 1)
                second = (successor_id, 0)
                self._add_arc(first, _Arc(second))
                self._add_arc(second, _Arc(first))
                connection_count += 1

        # Small pedestrian-only networks sometimes omit walking-area
        # connections.  In that representation, shared physical nodes are the
        # pedestrian topology.  Do not use this fallback for a full SUMO net
        # with walking areas because it could invent illegal junction turns.
        if not has_walkingarea or connection_count == 0:
            by_node: dict[str, list[Endpoint]] = defaultdict(list)
            for edge_id in self.walkable_edges:
                edge = self.edges[edge_id]
                by_node[edge.getFromNode().getID()].append((edge_id, 0))
                by_node[edge.getToNode().getID()].append((edge_id, 1))
            for endpoints in by_node.values():
                for first, second in itertools.combinations(endpoints, 2):
                    self._add_arc(first, _Arc(second))
                    self._add_arc(second, _Arc(first))

        self.counters["graph_nodes"] = len(self.adjacency)
        self.counters["graph_arcs"] = sum(len(items) for items in self.adjacency.values())

    def _add_arc(self, source: Endpoint, arc: _Arc) -> None:
        self.adjacency[source].append(arc)
        # Ensure isolated destination endpoints still appear as graph nodes.
        self.adjacency.setdefault(arc.to, [])

    def resolve_position(self, edge_id: str, value=None) -> EdgePosition:
        if edge_id not in self.edge_lengths:
            raise ValueError(f"unknown or non-pedestrian SUMO edge: {edge_id}")
        length = self.edge_lengths[edge_id]
        offset = length if value is None or value == "end" else float(value)
        if not math.isfinite(offset) or not 0.0 <= offset <= length:
            raise ValueError(
                f"invalid pedestrian position {value!r} on {edge_id} (length={length})"
            )
        return EdgePosition(edge_id, offset)

    def diagnostics(self) -> dict[str, int]:
        return {**self.counters, "cached_trees": len(self._trees)}

    def route(
        self,
        start: EdgePosition,
        target: EdgePosition,
        *,
        forbidden_edges: Iterable[str] = (),
        edge_costs: Mapping[str, float] | None = None,
        default_speed_mps: float | None = None,
        cost_token: Hashable | None = None,
    ) -> PositionRoute:
        """Return the least-cost route between two exact edge positions.

        Without ``edge_costs`` or ``default_speed_mps``, cost is distance in
        metres.  With a default speed, cost is seconds; entries in
        ``edge_costs`` replace the total traversal time for their edge.
        """
        self.counters["route_queries"] += 1
        start = self.resolve_position(start.edge_id, start.offset_m)
        target = self.resolve_position(target.edge_id, target.offset_m)
        forbidden = frozenset(map(str, forbidden_edges))
        best: tuple[float, float, tuple[_Arc, ...]] | None = None

        if start.edge_id == target.edge_id and start.edge_id not in forbidden:
            direct_distance = abs(target.offset_m - start.offset_m)
            direct_cost = self._partial_cost(
                start.edge_id, direct_distance, edge_costs, default_speed_mps
            )
            best = (direct_cost, direct_distance, ())

        start_length = self.edge_lengths[start.edge_id]
        target_length = self.edge_lengths[target.edge_id]
        source_options = ((0, start.offset_m), (1, start_length - start.offset_m))
        target_options = ((0, target.offset_m), (1, target_length - target.offset_m))
        for source_side, source_distance in source_options:
            source_endpoint = (start.edge_id, source_side)
            source_cost = self._partial_cost(
                start.edge_id, source_distance, edge_costs, default_speed_mps
            )
            for target_side, target_distance in target_options:
                target_endpoint = (target.edge_id, target_side)
                tree = self._tree(
                    target_endpoint,
                    forbidden,
                    edge_costs=edge_costs,
                    default_speed_mps=default_speed_mps,
                    cost_token=cost_token,
                )
                middle_cost = tree.distance.get(source_endpoint)
                if middle_cost is None:
                    continue
                target_cost = self._partial_cost(
                    target.edge_id, target_distance, edge_costs, default_speed_mps
                )
                arcs = self._arcs_to_root(source_endpoint, target_endpoint, tree)
                middle_distance = sum(
                    self.edge_lengths[arc.edge_id]
                    for arc in arcs
                    if arc.edge_id is not None
                )
                candidate = (
                    source_cost + middle_cost + target_cost,
                    source_distance + middle_distance + target_distance,
                    arcs,
                )
                if best is None or candidate[:2] < best[:2]:
                    best = candidate

        if best is None:
            raise PositionRouteUnavailable(
                f"no pedestrian route: {start.edge_id}@{start.offset_m:.2f} -> "
                f"{target.edge_id}@{target.offset_m:.2f}"
            )
        cost, distance, arcs = best
        return PositionRoute(
            self._public_edges(start.edge_id, target.edge_id, arcs),
            max(0.0, distance),
            max(0.0, cost),
        )

    def route_via_edges(
        self,
        start: EdgePosition,
        target: EdgePosition,
        via_edges: Iterable[str],
        *,
        via_orientations: Iterable[int | None] | None = None,
        forbidden_edges: Iterable[str] = (),
        edge_costs: Mapping[str, float] | None = None,
        default_speed_mps: float | None = None,
        cost_token: Hashable | None = None,
    ) -> PositionRoute:
        """Route through each portal edge exactly once, in the given order.

        Both orientations of every portal are evaluated.  This makes portal
        configuration independent of the underlying SUMO edge direction.
        """
        portals = tuple(map(str, via_edges))
        if not portals:
            return self.route(
                start,
                target,
                forbidden_edges=forbidden_edges,
                edge_costs=edge_costs,
                default_speed_mps=default_speed_mps,
                cost_token=cost_token,
            )
        self.counters["route_queries"] += 1
        for edge_id in portals:
            if edge_id not in self.edge_lengths:
                raise ValueError(f"unknown or non-pedestrian portal edge: {edge_id}")
        configured_orientations = (
            (None,) * len(portals)
            if via_orientations is None
            else tuple(via_orientations)
        )
        if len(configured_orientations) != len(portals):
            raise ValueError("via_orientations must match via_edges")
        if any(side not in {None, 0, 1} for side in configured_orientations):
            raise ValueError("portal orientations must be 0, 1 or None")
        orientation_options = tuple(
            (0, 1) if side is None else (side,)
            for side in configured_orientations
        )
        segment_forbidden = frozenset(map(str, forbidden_edges)) | frozenset(portals)
        best: PositionRoute | None = None
        for orientation in itertools.product(*orientation_options):
            try:
                parts: list[PositionRoute] = []
                first_entry = (portals[0], orientation[0])
                parts.append(self._position_to_endpoint(
                    start,
                    first_entry,
                    segment_forbidden,
                    edge_costs,
                    default_speed_mps,
                    cost_token,
                ))
                for index, edge_id in enumerate(portals):
                    enter_side = orientation[index]
                    exit_side = 1 - enter_side
                    portal_distance = self.edge_lengths[edge_id]
                    parts.append(PositionRoute(
                        (edge_id,) if self._is_public(edge_id) else (),
                        portal_distance,
                        self._partial_cost(
                            edge_id, portal_distance, edge_costs, default_speed_mps
                        ),
                    ))
                    if index + 1 < len(portals):
                        next_entry = (portals[index + 1], orientation[index + 1])
                        parts.append(self._endpoint_to_endpoint(
                            (edge_id, exit_side),
                            next_entry,
                            segment_forbidden,
                            edge_costs,
                            default_speed_mps,
                            cost_token,
                        ))
                    else:
                        parts.append(self._endpoint_to_position(
                            (edge_id, exit_side),
                            target,
                            segment_forbidden,
                            edge_costs,
                            default_speed_mps,
                            cost_token,
                        ))
                candidate = self._join(parts)
            except PositionRouteUnavailable:
                continue
            if best is None or (candidate.cost, candidate.distance_m) < (best.cost, best.distance_m):
                best = candidate
        if best is None:
            raise PositionRouteUnavailable(
                f"no pedestrian route via {portals}: {start.edge_id} -> {target.edge_id}"
            )
        return best

    def route_from_endpoint(
        self,
        endpoint: Endpoint,
        target: EdgePosition,
        *,
        forbidden_edges: Iterable[str] = (),
    ) -> PositionRoute:
        """Public diagnostic helper used to verify endpoint geometry."""
        return self._endpoint_to_position(
            endpoint,
            self.resolve_position(target.edge_id, target.offset_m),
            frozenset(map(str, forbidden_edges)),
            None,
            None,
            None,
        )

    def _position_to_endpoint(
        self,
        start: EdgePosition,
        endpoint: Endpoint,
        forbidden: frozenset[str],
        edge_costs,
        default_speed_mps,
        cost_token,
    ) -> PositionRoute:
        start = self.resolve_position(start.edge_id, start.offset_m)
        length = self.edge_lengths[start.edge_id]
        best = None
        tree = self._tree(
            endpoint,
            forbidden,
            edge_costs=edge_costs,
            default_speed_mps=default_speed_mps,
            cost_token=cost_token,
        )
        for side, partial_distance in ((0, start.offset_m), (1, length - start.offset_m)):
            source = (start.edge_id, side)
            middle_cost = tree.distance.get(source)
            if middle_cost is None:
                continue
            arcs = self._arcs_to_root(source, endpoint, tree)
            middle_distance = sum(
                self.edge_lengths[arc.edge_id] for arc in arcs if arc.edge_id is not None
            )
            candidate = (
                self._partial_cost(start.edge_id, partial_distance, edge_costs, default_speed_mps)
                + middle_cost,
                partial_distance + middle_distance,
                arcs,
            )
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            raise PositionRouteUnavailable(f"no pedestrian route to endpoint {endpoint}")
        cost, distance, arcs = best
        return PositionRoute(self._public_edges(start.edge_id, None, arcs), distance, cost)

    def _endpoint_to_position(
        self,
        endpoint: Endpoint,
        target: EdgePosition,
        forbidden: frozenset[str],
        edge_costs,
        default_speed_mps,
        cost_token,
    ) -> PositionRoute:
        target = self.resolve_position(target.edge_id, target.offset_m)
        length = self.edge_lengths[target.edge_id]
        best = None
        for side, partial_distance in ((0, target.offset_m), (1, length - target.offset_m)):
            root = (target.edge_id, side)
            tree = self._tree(
                root,
                forbidden,
                edge_costs=edge_costs,
                default_speed_mps=default_speed_mps,
                cost_token=cost_token,
            )
            middle_cost = tree.distance.get(endpoint)
            if middle_cost is None:
                continue
            arcs = self._arcs_to_root(endpoint, root, tree)
            middle_distance = sum(
                self.edge_lengths[arc.edge_id] for arc in arcs if arc.edge_id is not None
            )
            candidate = (
                middle_cost
                + self._partial_cost(target.edge_id, partial_distance, edge_costs, default_speed_mps),
                middle_distance + partial_distance,
                arcs,
            )
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            raise PositionRouteUnavailable(f"no pedestrian route from endpoint {endpoint}")
        cost, distance, arcs = best
        return PositionRoute(self._public_edges(None, target.edge_id, arcs), distance, cost)

    def _endpoint_to_endpoint(
        self,
        start: Endpoint,
        target: Endpoint,
        forbidden: frozenset[str],
        edge_costs,
        default_speed_mps,
        cost_token,
    ) -> PositionRoute:
        tree = self._tree(
            target,
            forbidden,
            edge_costs=edge_costs,
            default_speed_mps=default_speed_mps,
            cost_token=cost_token,
        )
        cost = tree.distance.get(start)
        if cost is None:
            raise PositionRouteUnavailable(f"no pedestrian route: {start} -> {target}")
        arcs = self._arcs_to_root(start, target, tree)
        distance = sum(
            self.edge_lengths[arc.edge_id] for arc in arcs if arc.edge_id is not None
        )
        return PositionRoute(self._public_edges(None, None, arcs), distance, cost)

    def _tree(
        self,
        root: Endpoint,
        forbidden: frozenset[str],
        *,
        edge_costs,
        default_speed_mps,
        cost_token,
    ) -> _Tree:
        if root not in self.adjacency:
            raise PositionRouteUnavailable(f"unknown pedestrian endpoint: {root}")
        cacheable = edge_costs is None or cost_token is not None
        if edge_costs is not None:
            token = ("dynamic", default_speed_mps, cost_token)
            if cost_token is not None and cost_token != self._dynamic_cost_token:
                self._trees = OrderedDict(
                    (key, value)
                    for key, value in self._trees.items()
                    if key[2][0] != "dynamic"
                )
                self._dynamic_cost_token = cost_token
        else:
            token = (
                "distance" if default_speed_mps is None
                else ("speed", default_speed_mps)
            )
        key = (root, forbidden, token)
        if cacheable and key in self._trees:
            self.counters["tree_cache_hits"] += 1
            self._trees.move_to_end(key)
            return self._trees[key]

        distance = {root: 0.0}
        next_step: dict[Endpoint, tuple[Endpoint, _Arc]] = {}
        queue = [(0.0, root)]
        while queue:
            current_distance, current = heapq.heappop(queue)
            if current_distance > distance[current] + 1e-12:
                continue
            for arc in self.adjacency[current]:
                if arc.edge_id is not None and arc.edge_id in forbidden:
                    continue
                weight = self._arc_cost(arc, edge_costs, default_speed_mps)
                candidate = current_distance + weight
                if candidate + 1e-12 >= distance.get(arc.to, float("inf")):
                    continue
                distance[arc.to] = candidate
                reverse = _Arc(
                    current,
                    arc.edge_id,
                    -arc.direction if arc.edge_id is not None else 0,
                )
                next_step[arc.to] = (current, reverse)
                heapq.heappush(queue, (candidate, arc.to))
        tree = _Tree(distance, next_step)
        self.counters["tree_builds"] += 1
        if cacheable:
            self._trees[key] = tree
            self._trees.move_to_end(key)
            while len(self._trees) > self.cache_limit:
                self._trees.popitem(last=False)
        return tree

    def _arc_cost(self, arc: _Arc, edge_costs, default_speed_mps) -> float:
        if arc.edge_id is None:
            return 0.0
        return self._partial_cost(
            arc.edge_id,
            self.edge_lengths[arc.edge_id],
            edge_costs,
            default_speed_mps,
        )

    def _partial_cost(self, edge_id, distance, edge_costs, default_speed_mps) -> float:
        length = self.edge_lengths[edge_id]
        if length <= 0.0:
            return 0.0
        if edge_costs is not None and edge_id in edge_costs:
            total = float(edge_costs[edge_id])
            if not math.isfinite(total) or total < 0.0:
                raise ValueError(f"invalid edge cost for {edge_id}: {total}")
        elif default_speed_mps is not None:
            speed = float(default_speed_mps)
            if not math.isfinite(speed) or speed <= 0.0:
                raise ValueError("default_speed_mps must be positive")
            total = length / speed
        else:
            total = length
        return total * max(0.0, distance) / length

    @staticmethod
    def _arcs_to_root(source: Endpoint, root: Endpoint, tree: _Tree) -> tuple[_Arc, ...]:
        arcs = []
        current = source
        seen = set()
        while current != root:
            if current in seen or current not in tree.next_step:
                raise PositionRouteUnavailable(f"incomplete shortest-path tree at {current}")
            seen.add(current)
            next_node, arc = tree.next_step[current]
            arcs.append(arc)
            current = next_node
        return tuple(arcs)

    def _public_edges(
        self,
        start_edge: str | None,
        target_edge: str | None,
        arcs: Iterable[_Arc],
    ) -> tuple[str, ...]:
        ordered: list[str] = []

        def append(edge_id: str | None) -> None:
            if edge_id is None or not self._is_public(edge_id):
                return
            if not ordered or ordered[-1] != edge_id:
                ordered.append(edge_id)

        append(start_edge)
        for arc in arcs:
            append(arc.edge_id)
        append(target_edge)
        return tuple(ordered)

    def _is_public(self, edge_id: str) -> bool:
        return edge_id in self.edges and not edge_id.startswith(":")

    @staticmethod
    def _join(parts: Iterable[PositionRoute]) -> PositionRoute:
        edges: list[str] = []
        distance = 0.0
        cost = 0.0
        for part in parts:
            for edge_id in part.edges:
                if not edges or edges[-1] != edge_id:
                    edges.append(edge_id)
            distance += part.distance_m
            cost += part.cost
        return PositionRoute(tuple(edges), distance, cost)
