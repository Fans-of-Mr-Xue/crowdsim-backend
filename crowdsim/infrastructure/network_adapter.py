"""SUMO-native network access with internal edges and official projection."""

from __future__ import annotations

import os
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

    def edge_function(self, edge_id: str) -> str:
        edge = self.edges.get(edge_id)
        return edge.getFunction() if edge is not None else "unknown"
