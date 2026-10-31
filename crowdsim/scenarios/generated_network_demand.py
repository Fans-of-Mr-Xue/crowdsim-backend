"""Reproducible initial pedestrian traffic on the selected SUMO network."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
import xml.etree.ElementTree as ET

from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.infrastructure.network_adapter import ResearchNetwork


@dataclass(frozen=True)
class NetworkDemandSpec:
    seed: int = 20260908
    default_count: int = 700
    max_count: int = 10000

    def validate_count(self, count):
        if type(count) is not int or not 0 <= count <= self.max_count:
            raise ValueError(f"population count must be an integer between 0 and {self.max_count}")

    def capabilities(self):
        return {"default_count": self.default_count, "count_min": 0,
                "count_max": self.max_count, "count_step": 1,
                "count_scope": "total_network_pedestrians", "background_count": 0}

    def generate(self, directory, network_path, requested_count=None):
        count = self.default_count if requested_count is None else requested_count
        self.validate_count(count)
        network_path = Path(network_path)
        network = ResearchNetwork(str(network_path))
        router = PositionAwarePedestrianRouter(network)
        roads = sorted(edge_id for edge_id in router.walkable_edges
                       if not edge_id.startswith(":") and router.edge_lengths[edge_id] >= 10)
        if len(roads) < 2:
            raise ValueError("network demand requires at least two usable pedestrian roads")
        weights = [(router.edge_lengths[road] - 4) * max(
            lane.getWidth() for lane in network.edges[road].getLanes() if lane.allows("pedestrian")
        ) for road in roads]
        rng = random.Random(self.seed)
        # Area-based allocation and stratified positions avoid piling every
        # depart=0 person at a road endpoint. All positions stay off junctions.
        starts = rng.choices(roads, weights=weights, k=count)
        allocation = {road: starts.count(road) for road in roads}
        positions = {}
        for road in roads:
            size = allocation[road]
            length = router.edge_lengths[road]
            positions[road] = [2 + (index + rng.random()) / size * (length - 4)
                               for index in range(size)]
            rng.shuffle(positions[road])
        root = ET.Element("routes")
        ET.SubElement(root, "vType", id="network_pedestrian", vClass="pedestrian", maxSpeed="1.3")
        distances = []
        for index, road in enumerate(starts):
            start = EdgePosition(road, positions[road].pop())
            for attempt in range(100):
                target_road = rng.choices(roads, weights=weights, k=1)[0]
                if target_road == road:
                    continue
                target = EdgePosition(target_road, rng.uniform(2, router.edge_lengths[target_road] - 2))
                route = router.route(start, target)
                if route.distance_m >= 50:
                    break
            else:
                raise ValueError(f"cannot generate a sufficiently long walk from {road}")
            person = ET.SubElement(root, "person", id=f"east-nanjing-{index:05d}",
                                   type="network_pedestrian", depart="0", departPos=f"{start.offset_m:.6f}")
            ET.SubElement(person, "walk", edges=" ".join(route.edges), arrivalPos=f"{target.offset_m:.6f}")
            distances.append(route.distance_m)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "network.generated.rou.xml"
        ET.indent(root, space="    ")
        ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
        report = {"generation_version": 1, "mode": "network_traffic", "seed": self.seed,
                  "initial_population": True, "requested_count": requested_count,
                  "effective_count": count, "total_count": count, "default_count": self.default_count,
                  "network_sha256": hashlib.sha256(network_path.read_bytes()).hexdigest(),
                  "spawn_allocation": allocation, "minimum_walk_distance_m": min(distances, default=0),
                  "demand_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
        (directory / "demand_generation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return output, report
