"""Add P21 parallel to P15 at 40% of the complete north-to-south P20.

The XML patch splits P14 and P20 at two real pedestrian junctions. Merge only
the affected topology; preserve geometry and widened walking areas at the four
original road endpoints. Hotspot configuration and demand need migration too.
"""

from __future__ import annotations

import argparse
import copy
import math
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.add_monument_m3 import canonical, render

SCENARIO = ROOT / "scenarios/shanghai_bund"
WEST = "huangpu_park_p21_west"
EAST = "huangpu_park_p21_east"
P14_SOUTH = "906417852#10"
P14_NORTH = "906417852#10_p21_north"
P20_NORTH = "huangpu_park_j05_j15"
P20_SOUTH = "huangpu_park_j05_j15_p21_south"
P21 = "huangpu_park_p21"
NEW_JUNCTIONS = {WEST, EAST}
BOUNDARIES = {"8417005110", "8417005111", "1883306110", "8417005113"}
JUNCTIONS = NEW_JUNCTIONS | BOUNDARIES
NEW_EDGES = {P14_NORTH, P20_SOUTH, P21}
SPLIT_ORIGINS = {
    P14_SOUTH: (P14_SOUTH, True), P14_NORTH: (P14_SOUTH, False),
    P20_NORTH: (P20_NORTH, True), P20_SOUTH: (P20_NORTH, False),
}
WALKING_AREA_WIDTHS = {WEST: 4.0, EAST: 4.0}
PROVENANCE = "<!-- Local P21 patch: scripts/add_park_p21.py; parallel to P15 at the north-to-south 40% point of P20. -->"


def internal_at(identifier: str, junctions=JUNCTIONS) -> bool:
    return any(identifier.startswith(f":{node}_") for node in junctions)


def connection_is_local(element: ET.Element) -> bool:
    return any(internal_at(element.get(key, "")) for key in ("from", "to", "via"))


def shape_points(lane: ET.Element) -> list[tuple[float, float]]:
    return [tuple(map(float, point.split(","))) for point in lane.get("shape").split()]


def shape_length(shape) -> float:
    return sum(math.dist(a, b) for a, b in zip(shape, shape[1:]))


def split_edge(candidate: ET.Element, original: ET.Element, keep_from: bool) -> ET.Element:
    result = copy.deepcopy(candidate)
    old_lane = original.find("lane")
    lane = result.find("lane")
    if len(result.findall("lane")) != 1 or len(original.findall("lane")) != 1:
        raise ValueError("local road patches require single-lane pedestrian roads")
    # Preserve original road semantics and the far-end lane cut, including its
    # centimetre precision, so existing widened boundary junctions still fit.
    for key in ("allow", "disallow", "speed", "width"):
        if key in old_lane.attrib:
            lane.set(key, old_lane.get(key))
    before = shape_points(lane)
    after = before[:]
    endpoint = 0 if keep_from else -1
    after[endpoint] = shape_points(old_lane)[endpoint]
    lane.set("shape", " ".join(f"{x:.3f},{y:.3f}" for x, y in after))
    lane.set("length", f"{float(lane.get('length')) + shape_length(after) - shape_length(before):.3f}")
    return result


def merge_local(
    base_text: str, candidate: ET.Element, *,
    new_junctions=NEW_JUNCTIONS, boundaries=BOUNDARIES, new_edges=NEW_EDGES,
    split_origins=SPLIT_ORIGINS, walking_area_widths=WALKING_AREA_WIDTHS,
    provenance=PROVENANCE, rebuilt_junctions=frozenset(), rebuilt_edges=frozenset(),
    terminal_junctions=frozenset(),
) -> str:
    """Merge a scoped pedestrian patch while retaining existing boundary geometry."""
    scoped_junctions = new_junctions | boundaries | rebuilt_junctions

    def is_internal(identifier):
        return internal_at(identifier, scoped_junctions)

    def is_local_connection(element):
        return any(is_internal(element.get(key, "")) for key in ("from", "to", "via"))

    base = ET.fromstring(base_text)
    originals = {edge.get("id"): edge for edge in base.findall("edge")}
    normal_ids = {
        edge.get("id") for edge in base.findall("edge")
        if edge.get("from") in scoped_junctions or edge.get("to") in scoped_junctions
    } | new_edges
    replacements = {}
    for edge in candidate.findall("edge"):
        identifier = edge.get("id")
        if identifier not in normal_ids and not is_internal(identifier):
            continue
        if identifier in split_origins:
            origin, keep_from = split_origins[identifier]
            edge = split_edge(edge, originals[origin], keep_from)
        elif identifier in originals and identifier not in rebuilt_edges and not internal_at(identifier, rebuilt_junctions):
            edge = copy.deepcopy(originals[identifier])
        else:
            edge = copy.deepcopy(edge)
        if edge.get("function") == "walkingarea" and internal_at(identifier, walking_area_widths):
            node = identifier[1:].rsplit("_w", 1)[0]
            lane = edge.find("lane")
            original = originals.get(identifier)
            old_width = float(original.find("lane").get("width")) if original is not None else 0
            lane.set("width", f"{max(walking_area_widths[node], old_width, float(lane.get('width'))):.2f}")
        replacements[identifier] = edge
    # A one-arm pedestrian dead end has no SUMO walkingarea; its lane reaches
    # the terminal node directly. Multi-arm junctions still require one.
    if not terminal_junctions.issubset(scoped_junctions):
        raise ValueError("terminal junctions must belong to the local patch")
    expected = normal_ids | {f":{node}_w0" for node in scoped_junctions - terminal_junctions}
    if not expected.issubset(replacements):
        raise ValueError(f"netconvert omitted local edges: {sorted(expected - replacements.keys())}")

    def replace_edge(match):
        old = ET.fromstring(match.group().strip())
        identifier = old.get("id")
        if identifier not in normal_ids and not is_internal(identifier):
            return match.group()
        new = replacements.pop(identifier, None)
        return match.group() if new is not None and canonical(old) == canonical(new) else render(new) if new is not None else ""

    result = re.sub(r"^    <edge\b[^>]*>.*?^    </edge>", replace_edge, base_text, flags=re.M | re.S)
    position = re.search(r"^    <(?:tlLogic|junction)\b", result, flags=re.M)
    added = "\n\n".join(render(replacements[key]) for key in sorted(replacements))
    result = result[:position.start()] + added + "\n\n" + result[position.start():]
    old_junctions = {node.get("id"): node for node in base.findall("junction")}
    junctions = {}
    for node in candidate.findall("junction"):
        identifier = node.get("id")
        if identifier not in scoped_junctions:
            continue
        if identifier in boundaries:
            previous = copy.deepcopy(old_junctions[identifier])
            for key in ("incLanes", "intLanes"):
                previous.set(key, node.get(key, ""))
            node = previous
        junctions[identifier] = node
    if set(junctions) != scoped_junctions:
        raise ValueError("netconvert omitted local junctions or road boundary nodes")

    def replace_junction(match):
        old = ET.fromstring(match.group().strip())
        new = junctions.pop(old.get("id"), None)
        return match.group() if new is None or canonical(new) == canonical(old) else render(new)

    result = re.sub(r"^    <junction\b[^\n]*?/>|^    <junction\b[^\n]*>.*?^    </junction>", replace_junction, result, flags=re.M | re.S)
    position = re.search(r"^    <connection\b", result, flags=re.M)
    added = "\n\n".join(render(junctions[key]) for key in sorted(junctions))
    result = result[:position.start()] + added + "\n\n" + result[position.start():]
    replacement_connections = {canonical(c): c for c in candidate.findall("connection") if is_local_connection(c)}

    def replace_connection(match):
        old = ET.fromstring(match.group().strip())
        if not is_local_connection(old):
            return match.group()
        new = replacement_connections.pop(canonical(old), None)
        return match.group() if new is not None else ""

    result = re.sub(r"^    <connection\b[^\n]*?/>\n?|^    <connection\b[^\n]*>.*?^    </connection>\n?", replace_connection, result, flags=re.M | re.S)
    position = re.search(r"^    <connection\b", result, flags=re.M)
    added = "\n".join(render(c) for c in replacement_connections.values())
    result = result[:position.start()] + added + "\n\n" + result[position.start():]
    result = result.replace("<net version=", provenance + "\n<net version=", 1)
    ET.fromstring(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--output", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--netconvert", default="netconvert")
    args = parser.parse_args()
    base_text = args.input.read_text(encoding="utf-8")
    root = ET.fromstring(base_text)
    present = {edge.get("id") for edge in root.findall("edge")}
    if present & NEW_EDGES:
        if not NEW_EDGES.issubset(present):
            raise ValueError("network contains an incomplete P21 patch")
        for node, width in WALKING_AREA_WIDTHS.items():
            lane = root.find(f"edge[@id=':{node}_w0']/lane")
            if lane is None or float(lane.get("width")) < width:
                raise ValueError(f"P21 widening is incomplete at {node}")
        print("P21 is already present; network left unchanged.")
        return
    with TemporaryDirectory(prefix="crowdsim-p21-") as directory:
        path = Path(directory) / "candidate.net.xml"
        subprocess.run([
            args.netconvert, "--sumo-net-file", str(args.input),
            "--node-files", str(SCENARIO / "patches/park_p21.nod.xml"),
            "--edge-files", str(SCENARIO / "patches/park_p21.edg.xml"),
            "--output-file", str(path), "--walkingareas", "true",
            "--offset.disable-normalization", "true", "--no-turnarounds", "true",
            "--junctions.corner-detail", "5", "--precision", "3",
        ], check=True, capture_output=True, text=True)
        result = merge_local(base_text, ET.parse(path).getroot())
    args.output.write_text(result, encoding="utf-8")
    print(f"P21 local patch written to {args.output}")


if __name__ == "__main__":
    main()
