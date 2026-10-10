"""SUMO-native network access with internal edges and official projection."""

from __future__ import annotations

import os
import math
from typing import Tuple

import sumolib


class ResearchNetwork:
    def __init__(self, net_path: str) -> None:
        self.net_path = os.path.abspath(net_path)
        self.net = sumolib.net.readNet(self.net_path, withInternal=True, withPedestrianConnections=True)
        self.edges = {edge.getID(): edge for edge in self.net.getEdges(withInternal=True)}

    @property
    def center(self) -> Tuple[float, float]:
        xmin, ymin, xmax, ymax = self.net.getBoundary()
        x, y = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        lon, lat = self.xy_to_lonlat(x, y)
        return lat, lon

    def xy_to_lonlat(self, x: float, y: float) -> Tuple[float, float]:
        try:
            lon, lat = self.net.convertXY2LonLat(x, y)
        except RuntimeError:
            lon, lat = x, y
        return float(lon), float(lat)

    def lonlat_to_xy(self, lon: float, lat: float) -> Tuple[float, float]:
        try:
            x, y = self.net.convertLonLat2XY(lon, lat)
        except RuntimeError:
            x, y = lon, lat
        return float(x), float(y)

    def pedestrian_edge_ids(self) -> set[str]:
        return {edge_id for edge_id, edge in self.edges.items() if any(lane.allows("pedestrian") for lane in edge.getLanes())}

    def lonlat_to_xy_strict(self, lon: float, lat: float) -> Tuple[float, float]:
        """Observation area calculations must never treat degrees as metres."""
        try:
            crs = self.net.getGeoProj().crs
            if not crs.is_projected or not crs.axis_info or any(
                not math.isclose(axis.unit_conversion_factor, 1.0) for axis in crs.axis_info
            ):
                raise ValueError("observation metrics require a metre-based projected SUMO network")
            x, y = self.net.convertLonLat2XY(lon, lat)
        except (RuntimeError, ImportError) as exc:
            raise ValueError("observation metrics require a geographically projected SUMO network") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("invalid observation projection result")
        return float(x), float(y)

    def edge_function(self, edge_id: str) -> str:
        edge = self.edges.get(edge_id)
        return edge.getFunction() if edge is not None else "unknown"
