"""Validated named crowd-observation areas on the loaded SUMO network."""

from __future__ import annotations

import json
from pathlib import Path

from crowdsim.infrastructure.network_adapter import ResearchNetwork


class HotspotCatalog:
    def __init__(self, network: ResearchNetwork, path: str | Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.parameter_status = str(payload.get("parameter_status", "unspecified"))
        self.hotspots = {}
        for raw in payload.get("hotspots", ()):
            item = dict(raw)
            hotspot_id = str(item.get("id", "")).strip()
            if not hotspot_id or hotspot_id in self.hotspots:
                raise ValueError("hotspot ids must be non-empty and unique")
            edges = tuple(str(edge_id) for edge_id in item.get("measurement_edges", ()))
            if not edges:
                raise ValueError(f"hotspot {hotspot_id} has no measurement_edges")
            for edge_id in edges:
                edge = network.edges.get(edge_id)
                if edge is None:
                    raise ValueError(f"hotspot {hotspot_id} references unknown edge {edge_id}")
                if not any(lane.allows("pedestrian") for lane in edge.getLanes()):
                    raise ValueError(f"hotspot {hotspot_id} edge does not allow pedestrians: {edge_id}")
            target = str(item.get("target_edge", ""))
            if target not in edges:
                raise ValueError(f"hotspot {hotspot_id} target_edge must be measured")
            item["measurement_edges"] = edges
            self.hotspots[hotspot_id] = item

    def serialize(self) -> list[dict]:
        result = []
        for item in self.hotspots.values():
            result.append({
                "id": item["id"],
                "name": item.get("name", item["id"]),
                "anchor": dict(item.get("anchor", {})),
                "target_edge": item["target_edge"],
                "measurement_edges": list(item["measurement_edges"]),
                "parameter_status": self.parameter_status,
            })
        return result
