import math
import os
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

from pyproj import CRS, Transformer

from crowdsim_models import RouteTemplate, Segment


VEHICLE_CLASSES = {
    "passenger", "private", "taxi", "bus", "coach", "truck", "trailer",
    "motorcycle", "moped", "delivery",
}


def parse_shape(shape: str) -> List[Tuple[float, float]]:
    points = []
    for pair in shape.split():
        try:
            x_raw, y_raw = pair.split(",", 1)
            points.append((float(x_raw), float(y_raw)))
        except ValueError:
            continue
    return points


def polyline_length(points: List[Tuple[float, float]]) -> float:
    return sum(math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]) for i in range(1, len(points)))


def point_at_distance(points: List[Tuple[float, float]], distance: float) -> Tuple[float, float, float, float]:
    if len(points) < 2:
        x, y = points[0] if points else (0.0, 0.0)
        return x, y, 1.0, 0.0
    remaining = max(0.0, distance)
    last_ux, last_uy = 1.0, 0.0
    for index in range(len(points) - 1):
        x1, y1 = points[index]
        x2, y2 = points[index + 1]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length <= 0.0:
            continue
        last_ux, last_uy = dx / length, dy / length
        if remaining <= length:
            return x1 + last_ux * remaining, y1 + last_uy * remaining, last_ux, last_uy
        remaining -= length
    return points[-1][0], points[-1][1], last_ux, last_uy


def thin_points(points: List[Tuple[float, float]], limit: int) -> List[Tuple[float, float]]:
    if len(points) <= limit:
        return points
    stride = max(1, math.ceil(len(points) / limit))
    return points[::stride][:limit]


class SumoNetAdapter:
    def __init__(self, net_path: str) -> None:
        self.net_path = os.path.abspath(net_path)
        self.segments: Dict[str, Segment] = {}
        self.center_lat, self.center_lon = 31.2417, 121.4905
        self._offset_x = self._offset_y = 0.0
        self._to_geo: Optional[Transformer] = None
        self._to_xy: Optional[Transformer] = None

    def load(self) -> None:
        for event, elem in ElementTree.iterparse(self.net_path, events=("start", "end")):
            if event == "start" and elem.tag == "location":
                self._load_projection(elem.attrib)
            elif event == "end" and elem.tag == "edge":
                segment = self._parse_edge(elem)
                if segment is not None:
                    self.segments[segment.id] = segment
                elem.clear()
            elif event == "end" and elem.tag == "connection":
                from_id, to_id = elem.attrib.get("from", ""), elem.attrib.get("to", "")
                if from_id in self.segments and to_id in self.segments:
                    self.segments[from_id].outgoing.append(to_id)
                elem.clear()
        self._calc_center()

    def _load_projection(self, attrib: Dict[str, str]) -> None:
        try:
            self._offset_x, self._offset_y = map(float, attrib.get("netOffset", "0,0").split(",", 1))
        except ValueError:
            self._offset_x = self._offset_y = 0.0
        if attrib.get("projParameter"):
            crs = CRS.from_proj4(attrib["projParameter"])
            self._to_geo = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
            self._to_xy = Transformer.from_crs(CRS.from_epsg(4326), crs, always_xy=True)

    def _parse_edge(self, edge: ElementTree.Element) -> Optional[Segment]:
        edge_id = edge.attrib.get("id", "")
        if not edge_id or edge.attrib.get("function") or not edge.attrib.get("from") or not edge.attrib.get("to"):
            return None
        lane = next((item for item in edge.findall("lane") if item.attrib.get("shape")), None)
        if lane is None:
            return None
        points = parse_shape(lane.attrib.get("shape", ""))
        if len(points) < 2:
            return None
        allow = set(lane.attrib.get("allow", "").split())
        disallow = set(lane.attrib.get("disallow", "").split())
        pedestrian = "pedestrian" in allow
        vehicle = (not allow or bool(allow & VEHICLE_CLASSES)) and "passenger" not in disallow and "all" not in disallow
        if not pedestrian and not vehicle:
            return None
        return Segment(
            edge_id, edge.attrib["from"], edge.attrib["to"], points,
            max(1.0, float(lane.attrib.get("length") or polyline_length(points))),
            max(0.2, float(lane.attrib.get("speed") or (1.35 if pedestrian else 8.0))),
            max(1.0, float(lane.attrib.get("width") or (3.2 if pedestrian else 3.5))),
            pedestrian, vehicle,
        )

    def _calc_center(self) -> None:
        points = [point for segment in self.segments.values() for point in segment.points]
        if points:
            xs, ys = zip(*points)
            self.center_lon, self.center_lat = self.xy_to_lonlat((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)

    def xy_to_lonlat(self, x: float, y: float) -> Tuple[float, float]:
        if self._to_geo:
            lon, lat = self._to_geo.transform(x - self._offset_x, y - self._offset_y)
            return float(lon), float(lat)
        return self.center_lon + x / (111320 * math.cos(math.radians(self.center_lat))), self.center_lat + y / 111320

    def lonlat_to_xy(self, lon: float, lat: float) -> Tuple[float, float]:
        if self._to_xy:
            x, y = self._to_xy.transform(lon, lat)
            return float(x + self._offset_x), float(y + self._offset_y)
        return (lon - self.center_lon) * 111320 * math.cos(math.radians(self.center_lat)), (lat - self.center_lat) * 111320


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_vehicle_routes(path: str, segments: Dict[str, Segment]) -> List[RouteTemplate]:
    return _load_routes(path, segments, "vehicle", "vehicle", "route")


def load_pedestrian_routes(path: str, segments: Dict[str, Segment]) -> List[RouteTemplate]:
    return _load_routes(path, segments, "person", "pedestrian", "walk")


def _load_routes(path: str, segments: Dict[str, Segment], tag: str, capability: str, child_tag: str) -> List[RouteTemplate]:
    routes = []
    for _, elem in ElementTree.iterparse(path, events=("end",)):
        if elem.tag == tag:
            child = elem.find(child_tag)
            edges = child.attrib.get("edges", "").split() if child is not None else []
            valid = [edge for edge in edges if edge in segments and getattr(segments[edge], capability)]
            if len(valid) >= 2:
                routes.append(RouteTemplate(elem.attrib.get("id", str(len(routes))), _safe_float(elem.attrib.get("depart"), 0.0), valid))
            elem.clear()
    return routes
