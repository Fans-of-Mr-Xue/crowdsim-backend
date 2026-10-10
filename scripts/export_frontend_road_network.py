"""Export SUMO road surfaces for a separately deployed frontend; no SUMO run.

The JSON uses WGS84 [longitude, latitude]. Copy it to the frontend's
public/static/crowd_sim/road_network.json after each network edit.
Install the optional geometry dependencies from requirements-road-export.txt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
FORMAT = "crowdsim-road-network"
SCHEMA_VERSION = 1


def parse_shape(value: str | None) -> list[tuple[float, float]]:
    points = []
    for token in (value or "").split():
        pair = tuple(float(number) for number in token.split(","))
        if len(pair) != 2 or not all(math.isfinite(number) for number in pair):
            raise ValueError(f"Invalid SUMO shape point: {token}")
        points.append(pair)
    return points


def polygons(geometry):
    if geometry.is_empty:
        return
    if geometry.geom_type == "Polygon":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from polygons(part)


def build_road_network(network_path: Path) -> dict:
    try:
        from pyproj import CRS, Transformer
        from shapely import make_valid, normalize
        from shapely.geometry import LineString, Polygon
        from shapely.ops import unary_union
    except ImportError as error:
        raise RuntimeError(
            "Install export dependencies: python -m pip install -r requirements-road-export.txt"
        ) from error

    network_bytes = network_path.read_bytes()
    root = ET.fromstring(network_bytes)
    if root.tag != "net":
        raise ValueError("Expected a SUMO .net.xml file")
    location = root.find("location")
    if location is None:
        raise ValueError("SUMO network has no location/projection metadata")
    projection = location.get("projParameter", "")
    if projection in ("", "!", "-", "."):
        raise ValueError("A geographic SUMO projection is required; cannot assume WGS84")
    offsets = parse_shape(location.get("netOffset"))
    if len(offsets) != 1:
        raise ValueError("SUMO location must specify one netOffset")
    offset_x, offset_y = offsets[0]
    to_wgs84 = Transformer.from_crs(CRS.from_user_input(projection), "EPSG:4326", always_xy=True)

    def lonlat(path):
        result = []
        for x, y in path:
            lng, lat = to_wgs84.transform(x - offset_x, y - offset_y, errcheck=True)
            if not (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90):
                raise ValueError(f"Invalid projected coordinate: {x}, {y}")
            result.append([round(lng, 9), round(lat, 9)])
        return result

    surfaces = []
    repaired = 0
    empty = 0

    def add_surface(geometry):
        nonlocal repaired, empty
        if not geometry.is_valid:
            geometry = make_valid(geometry)
            repaired += 1
        parts = list(polygons(geometry))
        if not parts:
            empty += 1
        surfaces.extend(parts)

    segments = []
    walking_areas = []
    crossing_count = 0
    degenerate_walking_areas = 0
    for edge in root.findall("edge"):
        function = edge.get("function", "normal")
        # Vehicle turn connectors are already covered by junction polygons.
        if function not in ("normal", "crossing", "walkingarea"):
            continue
        lanes = []
        for lane in edge.findall("lane"):
            shape = parse_shape(lane.get("shape"))
            width = float(lane.get("width", "3.2"))
            if not math.isfinite(width) or width <= 0:
                raise ValueError(f"Invalid lane width: {lane.get('id')}")
            if len(shape) < 2:
                raise ValueError(f"Lane has no usable shape: {lane.get('id')}")
            lane_data = {
                "laneId": lane.get("id"),
                "widthMeters": width,
                "lengthMeters": float(lane.get("length", "0")),
                "path": lonlat(shape),
            }
            if function == "walkingarea":
                # SUMO walkingarea shapes are polygons, NOT line centerlines.
                if len(shape) >= 3:
                    add_surface(Polygon(shape))
                else:
                    # Existing SUMO nets can contain two-point, zero-area
                    # walking areas. Keep their metadata; adjacent junction
                    # and lane surfaces supply the drawable geometry.
                    degenerate_walking_areas += 1
                walking_areas.append({"edgeId": edge.get("id"), **lane_data})
            else:
                # Buffer in SUMO meters before projecting to degrees. Flat ends
                # join the actual junction areas rather than adding round caps.
                add_surface(LineString(shape).buffer(width / 2, cap_style=2, join_style=2, mitre_limit=2))
                lanes.append(lane_data)
        if function == "normal":
            segments.append({
                "edgeId": edge.get("id"),
                "fromJunction": edge.get("from"),
                "toJunction": edge.get("to"),
                "type": edge.get("type", ""),
                "lanes": lanes,
            })
        elif function == "crossing":
            crossing_count += 1

    junctions = []
    for junction in root.findall("junction"):
        if junction.get("type") == "internal":
            continue
        shape = parse_shape(junction.get("shape"))
        if len(shape) >= 3:
            add_surface(Polygon(shape))
        junctions.append({
            "junctionId": junction.get("id"),
            "type": junction.get("type"),
            "position": lonlat([(float(junction.get("x")), float(junction.get("y")))])[0],
            "shape": lonlat(shape),
        })

    if not surfaces:
        raise ValueError("Network contains no road or junction surfaces")
    merged = normalize(unary_union(surfaces))
    road_lines = []
    for polygon in polygons(merged):
        road_lines.append(lonlat(polygon.exterior.coords))
        road_lines.extend(lonlat(ring.coords) for ring in polygon.interiors)
    coordinates = [point for line in road_lines for point in line]
    return {
        "format": FORMAT,
        "schemaVersion": SCHEMA_VERSION,
        "coordinateSystem": "wgs84",
        "coordinateOrder": "lng,lat",
        "source": {
            "file": network_path.name,
            "sha256": hashlib.sha256(network_bytes).hexdigest(),
            "sumoNetVersion": root.get("version"),
            "projection": projection,
            "netOffset": [offset_x, offset_y],
        },
        "counts": {
            "normalEdges": len(segments),
            "normalLanes": sum(len(edge["lanes"]) for edge in segments),
            "walkingAreas": len(walking_areas),
            "crossings": crossing_count,
            "junctions": len(junctions),
            "roadOutlines": len(road_lines),
            "outlineVertices": len(coordinates),
            "repairedSurfaces": repaired,
            "emptySurfaces": empty,
            "degenerateWalkingAreas": degenerate_walking_areas,
        },
        "bounds": [
            min(point[0] for point in coordinates), min(point[1] for point in coordinates),
            max(point[0] for point in coordinates), max(point[1] for point in coordinates),
        ],
        "roadLines": road_lines,
        "roadSegments": segments,
        "walkingAreas": walking_areas,
        "junctions": junctions,
    }


def export_road_network(network_path: Path, output_path: Path) -> dict:
    if network_path.resolve() == output_path.resolve():
        raise ValueError("Export output must not overwrite the SUMO network")
    payload = build_road_network(network_path)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialized, encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", type=Path, default=ROOT / "scenarios/shanghai_bund/bund.net.xml")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/frontend/road_network.json")
    args = parser.parse_args()
    try:
        payload = export_road_network(args.network, args.output)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Road export failed: {error}\n")
    print(json.dumps({
        "output": str(args.output.resolve()),
        "sourceSha256": payload["source"]["sha256"],
        "coordinateSystem": payload["coordinateSystem"],
        "counts": payload["counts"],
        "bytes": args.output.stat().st_size,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
