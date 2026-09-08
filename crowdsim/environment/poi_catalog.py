"""Validated finite activity and exit targets."""

from __future__ import annotations

import json
from pathlib import Path

from crowdsim.infrastructure.network_adapter import ResearchNetwork


class PoiCatalog:
    def __init__(self, network: ResearchNetwork, path: str | Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.pois = {item["id"]: item for item in payload["pois"]}
        for poi in self.pois.values():
            edge = poi["edge"]
            if edge not in network.edges:
                raise ValueError(f"POI {poi['id']} references unknown edge {edge}")
            if not any(lane.allows("pedestrian") for lane in network.edges[edge].getLanes()):
                raise ValueError(f"POI {poi['id']} edge does not allow pedestrians")
        if sum(item["kind"] == "activity" for item in self.pois.values()) < 2:
            raise ValueError("at least two activity POIs are required")
        if not any(item["kind"] == "exit" for item in self.pois.values()):
            raise ValueError("at least one exit POI is required")

    def available(self, now: float, known_ids=None) -> list[dict]:
        known = set(known_ids or self.pois)
        return [item for item in self.pois.values() if item["id"] in known and float(item["open"][0]) <= now <= float(item["open"][1])]
