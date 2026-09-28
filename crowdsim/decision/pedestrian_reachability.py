"""Permission-filtered pedestrian topology, indexed once and searched per target.

This is a conservative prefilter, not a replacement for SUMO routing.  Crossing
and walkingarea edges must be loaded with withPedestrianConnections=True.
"""

from collections import defaultdict, deque
import math


def walking_length(network, edge_id: str) -> float:
    edge = network.edges.get(edge_id)
    if edge is None:
        raise ValueError(f"unknown SUMO edge: {edge_id}")
    lanes = [lane for lane in edge.getLanes() if lane.allows("pedestrian")]
    if not lanes:
        raise ValueError(f"edge does not allow pedestrians: {edge_id}")
    return min(float(edge.getLength()), *(float(lane.getLength()) for lane in lanes))


def resolve_position(network, edge_id: str, value=None) -> float:
    length = walking_length(network, edge_id)
    position = length if value is None or value == "end" else float(value)
    if not math.isfinite(position) or not 0 <= position <= length:
        raise ValueError(f"invalid pedestrian position {value!r} on {edge_id} (length={length})")
    # SUMO's route query rejects an exact edge endpoint, and net XML lengths
    # are rounded. Keep a centimetre inside the pedestrian lane.
    return min(position, max(0.0, length - min(0.01, length / 2.0)))


class PedestrianReachability:
    def __init__(self, network):
        self.walkable = network.pedestrian_edge_ids()
        self.reverse = defaultdict(set)
        self.neighbours = {}
        by_node = defaultdict(set)
        has_walkingarea = any(edge.getFunction() == "walkingarea" for edge in network.edges.values())
        if not has_walkingarea:
            for edge_id in self.walkable:
                edge = network.edges[edge_id]
                if not edge.getFunction():
                    by_node[edge.getFromNode().getID()].add(edge_id)
                    by_node[edge.getToNode().getID()].add(edge_id)
        for edge_id in self.walkable:
            edge = network.edges[edge_id]
            # Walking can use the opposite direction.  Unlike sumolib's raw
            # ignoreDirection path, incoming neighbours are permission-filtered.
            adjacent = {item.getID() for item in edge.getAllowedOutgoing("pedestrian")}
            adjacent.update(item.getID() for item in edge.getIncoming())
            if not has_walkingarea and not edge.getFunction():
                adjacent.update(by_node[edge.getFromNode().getID()])
                adjacent.update(by_node[edge.getToNode().getID()])
            adjacent.intersection_update(self.walkable)
            self.neighbours[edge_id] = adjacent
            for successor in adjacent:
                self.reverse[successor].add(edge_id)
        self._targets = {}

    def can_reach(self, from_edge: str, to_edge: str) -> bool:
        if from_edge not in self.walkable or to_edge not in self.walkable:
            return False
        if to_edge not in self._targets:
            reached = {to_edge}
            queue = deque([to_edge])
            while queue:
                for predecessor in self.reverse[queue.popleft()]:
                    if predecessor not in reached:
                        reached.add(predecessor)
                        queue.append(predecessor)
            self._targets[to_edge] = reached
        return from_edge in self._targets[to_edge]

    def diagnostics(self):
        return {"walkable_edges": len(self.walkable), "indexed_targets": len(self._targets)}
