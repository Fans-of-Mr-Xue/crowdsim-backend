"""Add the straight P20 footway, rebuilding only J05/J15 pedestrian geometry.

Keep the base network version, unrelated elements and manual widening. Existing
incident lanes retain their far-end geometry and all non-geometric attributes.
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

SCENARIO = ROOT / "scenarios" / "shanghai_bund"
EDGE_ID = "huangpu_park_j05_j15"
JUNCTIONS = {"1883306110", "8417005113"}
# J15 becomes a four-arm junction. The existing 4m setting failed the
# all-movements SUMO check; 5m passed with no automatic jam resolution.
WALKING_AREA_WIDTHS = {":1883306110_w0": 4.0, ":8417005113_w0": 5.0}
PROVENANCE = "<!-- Local P20 patch: scripts/add_park_j05_j15.py; user-requested straight J05-J15 footway. -->"


def local_internal(identifier: str) -> bool:
    return any(identifier.startswith(f":{node}_") for node in JUNCTIONS)


def local_connection(element: ET.Element) -> bool:
    return any(
        local_internal(element.get(key, "")) or element.get(key) == EDGE_ID
        for key in ("from", "to", "via")
    )


def points(lane: ET.Element) -> list[tuple[float, float]]:
    return [tuple(map(float, point.split(","))) for point in lane.get("shape").split()]


def polyline_length(shape: list[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(shape, shape[1:]))


def update_incident_edge(previous: ET.Element, candidate: ET.Element) -> ET.Element:
    result = copy.deepcopy(previous)
    old_lanes, new_lanes = result.findall("lane"), candidate.findall("lane")
    if len(old_lanes) != len(new_lanes):
        raise ValueError("P20 patch must not change existing lane counts")
    for old, new in zip(old_lanes, new_lanes):
        old_shape, new_shape = points(old), points(new)
        # These local edits move only lane endpoints, along the original road.
        if old_shape[1:-1] != new_shape[1:-1]:
            raise ValueError(f"netconvert changed intermediate geometry: {previous.get('id')}")
        shape = old_shape[:]
        if previous.get("from") in JUNCTIONS:
            shape[0] = new_shape[0]
        if previous.get("to") in JUNCTIONS:
            shape[-1] = new_shape[-1]
        if shape == old_shape:
            continue
        length = float(old.get("length")) + polyline_length(shape) - polyline_length(old_shape)
        if length <= 0:
            raise ValueError(f"non-positive lane length: {old.get('id')}")
        old.set("shape", " ".join(f"{x:.2f},{y:.2f}" for x, y in shape))
        old.set("length", f"{length:.2f}")
    return result


def merge_local(base_text: str, candidate: ET.Element) -> str:
    base = ET.fromstring(base_text)
    originals = {edge.get("id"): edge for edge in base.findall("edge")}
    normal_ids = {
        edge.get("id") for edge in base.findall("edge")
        if edge.get("from") in JUNCTIONS or edge.get("to") in JUNCTIONS
    } | {EDGE_ID}
    replacements = {}
    for edge in candidate.findall("edge"):
        identifier = edge.get("id")
        if identifier not in normal_ids and not local_internal(identifier):
            continue
        edge = copy.deepcopy(edge)
        previous = originals.get(identifier)
        if identifier in normal_ids and previous is not None:
            edge = update_incident_edge(previous, edge)
        if edge.get("function") == "walkingarea":
            old_lane = previous.find("lane") if previous is not None else None
            width = max(WALKING_AREA_WIDTHS[identifier], float(old_lane.get("width")) if old_lane is not None else 0)
            for lane in edge.findall("lane"):
                lane.set("width", f"{width:.2f}")
        replacements[identifier] = edge
    expected = normal_ids | {f":{node}_w0" for node in JUNCTIONS}
    if not expected.issubset(replacements):
        raise ValueError(f"netconvert omitted local edges: {sorted(expected - replacements.keys())}")

    def replace_edge(match):
        element = ET.fromstring(match.group().strip())
        identifier = element.get("id")
        if identifier not in normal_ids and not local_internal(identifier):
            return match.group()
        replacement = replacements.pop(identifier, None)
        if replacement is not None and canonical(element) == canonical(replacement):
            return match.group()
        return render(replacement) if replacement is not None else ""

    result = re.sub(r"^    <edge\b[^>]*>.*?^    </edge>", replace_edge, base_text, flags=re.M | re.S)
    position = re.search(r"^    <(?:tlLogic|junction)\b", result, flags=re.M)
    new_edges = "\n\n".join(render(replacements[key]) for key in sorted(replacements))
    result = result[:position.start()] + new_edges + "\n\n" + result[position.start():]

    junctions = {node.get("id"): node for node in candidate.findall("junction") if node.get("id") in JUNCTIONS}
    if set(junctions) != JUNCTIONS:
        raise ValueError("netconvert omitted J05/J15 junctions")

    def replace_junction(match):
        element = ET.fromstring(match.group().strip())
        replacement = junctions.get(element.get("id"))
        return render(replacement) if replacement is not None else match.group()

    result = re.sub(r"^    <junction\b[^\n]*?/>|^    <junction\b[^\n]*>.*?^    </junction>", replace_junction, result, flags=re.M | re.S)
    connections = [render(c) for c in candidate.findall("connection") if local_connection(c)]

    def replace_connection(match):
        element = ET.fromstring(match.group().strip())
        return "" if local_connection(element) else match.group()

    result = re.sub(r"^    <connection\b[^\n]*?/>\n?|^    <connection\b[^\n]*>.*?^    </connection>\n?", replace_connection, result, flags=re.M | re.S)
    position = re.search(r"^    <connection\b", result, flags=re.M)
    result = result[:position.start()] + "\n".join(connections) + "\n\n" + result[position.start():]
    result = result.replace("<net version=", PROVENANCE + "\n<net version=", 1)
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
    existing = root.find(f"edge[@id='{EDGE_ID}']")
    if existing is not None:
        south = root.find("edge[@id='huangpu_park_j05_j15_p21_south']")
        last = root.find("edge[@id='huangpu_park_j05_j15_p21_south_p25_south']")
        complete = existing.get("from") == "1883306110" and (
            existing.get("to") == "8417005113" or (
                south is not None and south.get("from") == existing.get("to")
                and (south.get("to") == "8417005113" or (
                    last is not None and last.get("from") == south.get("to")
                    and last.get("to") == "8417005113"
                ))
            )
        )
        if not complete:
            raise ValueError("existing P20 has different endpoint junctions")
        for identifier, minimum in WALKING_AREA_WIDTHS.items():
            lane = root.find(f"edge[@id='{identifier}']/lane")
            if lane is None or float(lane.get("width")) < minimum:
                raise ValueError(f"existing P20 has an incomplete walking-area widening: {identifier}")
        print("P20 is already present; network left unchanged.")
        return
    with TemporaryDirectory(prefix="crowdsim-p20-") as directory:
        candidate_path = Path(directory) / "candidate.net.xml"
        subprocess.run([
            args.netconvert, "--sumo-net-file", str(args.input),
            "--edge-files", str(SCENARIO / "patches" / "park_j05_j15.edg.xml"),
            "--output-file", str(candidate_path), "--walkingareas", "true",
            "--offset.disable-normalization", "true", "--no-turnarounds", "true",
            "--junctions.corner-detail", "5",
        ], check=True, capture_output=True, text=True)
        result = merge_local(base_text, ET.parse(candidate_path).getroot())
    args.output.write_text(result, encoding="utf-8")
    print(f"P20 local patch written to {args.output}")


if __name__ == "__main__":
    main()
