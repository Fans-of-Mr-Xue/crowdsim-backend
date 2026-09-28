"""Validated finite activity and exit targets."""

from __future__ import annotations

import json
import math
from pathlib import Path

from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.decision.pedestrian_reachability import resolve_position


class PoiCatalog:
    def __init__(self, network: ResearchNetwork, path: str | Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.pois = {item["id"]: item for item in payload["pois"]}
        if len(self.pois) != len(payload["pois"]):
            raise ValueError("duplicate POI id")
        for poi in self.pois.values():
            edge = poi["edge"]
            if edge not in network.edges:
                raise ValueError(f"POI {poi['id']} references unknown edge {edge}")
            if not any(lane.allows("pedestrian") for lane in network.edges[edge].getLanes()):
                raise ValueError(f"POI {poi['id']} edge does not allow pedestrians")
            if poi.get("kind") not in {"activity", "exit"}:
                raise ValueError(f"invalid POI kind: {poi['id']}")
            poi["arrival_position"] = resolve_position(network, edge, poi.get("position", "end"))
            for key in ("open", "stay_seconds"):
                if key == "open" and key not in poi:
                    raise ValueError(f"missing open interval for POI {poi['id']}")
                interval = poi.get(key, [0, 0])
                if len(interval) != 2 or not all(math.isfinite(float(item)) for item in interval):
                    raise ValueError(f"invalid {key} for POI {poi['id']}")
                if float(interval[0]) < 0 or float(interval[1]) < float(interval[0]):
                    raise ValueError(f"invalid {key} for POI {poi['id']}")
            if poi["kind"] == "activity" and float(poi.get("stay_seconds", [0])[0]) <= 0:
                raise ValueError(f"activity POI requires positive stay: {poi['id']}")
        if sum(item["kind"] == "activity" for item in self.pois.values()) < 2:
            raise ValueError("at least two activity POIs are required")
        if not any(item["kind"] == "exit" for item in self.pois.values()):
            raise ValueError("at least one exit POI is required")

    def available(self, now: float, known_ids=None) -> list[dict]:
        known = set(self.pois if known_ids is None else known_ids)
        return [item for item in self.pois.values() if item["id"] in known and float(item["open"][0]) <= now <= float(item["open"][1])]
