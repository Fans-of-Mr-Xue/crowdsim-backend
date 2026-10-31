"""Add the northeast J33 branch perpendicular to J15-J28, half of J15-J13.

Lengths use complete junction-to-junction centerlines, before walkingarea
lane cuts. J37 is a free endpoint. Rebuild the four-arm J33 while preserving
all neighboring walkingarea polygons and original far-end lane cuts.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.add_park_p21 import merge_local as merge_scoped, EAST, P20_SOUTH
from scripts.add_park_p25_p26 import P20_MIDPOINT, P20_LAST, P25, MIDPOINT

SCENARIO = ROOT / "scenarios/shanghai_bund"
P28 = "huangpu_park_p28"
TERMINUS = "huangpu_park_p28_end"
J15 = "8417005113"
J13 = "8417005112"
LENGTH_REFERENCE = "906417853#1"
NEW_EDGES = {P28}
NEW_JUNCTIONS = {TERMINUS}
REBUILT_JUNCTIONS = {P20_MIDPOINT}
BOUNDARIES = {EAST, J15, MIDPOINT}
JUNCTIONS = NEW_JUNCTIONS | REBUILT_JUNCTIONS | BOUNDARIES
SPLIT_ORIGINS = {
    P20_SOUTH: (P20_SOUTH, True), P20_LAST: (P20_LAST, False), P25: (P25, True),
}
WALKING_AREA_WIDTHS = {P20_MIDPOINT: 4.0}
PROVENANCE = "<!-- Local P28 patch: scripts/add_park_p28.py; northeast J33-J37 footway perpendicular to J15-J28, half the complete J15-J13 centerline length. -->"


def coordinate(root: ET.Element, node: str) -> tuple[float, float]:
    junction = root.find(f"junction[@id='{node}']")
    return float(junction.get("x")), float(junction.get("y"))


def full_centerline(root: ET.Element, key: str) -> list[tuple[float, float]]:
    edge = root.find(f"edge[@id='{key}']")
    shape = edge.get("shape") or edge.find("lane").get("shape")
    return [coordinate(root, edge.get("from")),
            *[tuple(map(float, p.split(","))) for p in shape.split()],
            coordinate(root, edge.get("to"))]


def expected_endpoint(root: ET.Element) -> tuple[float, float]:
    j15, j28, start = (coordinate(root, n) for n in (J15, EAST, P20_MIDPOINT))
    direction = tuple(j28[i] - j15[i] for i in (0, 1))
    span = math.hypot(*direction)
    northeast = (direction[1] / span, -direction[0] / span)
    if not all(x > 0 for x in northeast):
        raise ValueError("The selected perpendicular must point northeast")
    reference = full_centerline(root, LENGTH_REFERENCE)
    length = sum(math.dist(a, b) for a, b in zip(reference, reference[1:])) / 2
    return tuple(start[i] + length * northeast[i] for i in (0, 1))


def merge_local(base_text: str, candidate: ET.Element) -> str:
    return merge_scoped(
        base_text, candidate, new_junctions=NEW_JUNCTIONS, boundaries=BOUNDARIES,
        rebuilt_junctions=REBUILT_JUNCTIONS, terminal_junctions={TERMINUS},
        new_edges=NEW_EDGES, split_origins=SPLIT_ORIGINS,
        walking_area_widths=WALKING_AREA_WIDTHS, provenance=PROVENANCE,
    )


def validate_applied(root: ET.Element) -> None:
    for node, neighbours in ((P20_MIDPOINT, {P20_SOUTH, P20_LAST, P25, P28}), (TERMINUS, {P28})):
        actual = {e.get("id") for e in root.findall("edge") if node in (e.get("from"), e.get("to"))}
        if actual != neighbours:
            raise ValueError(f"P28 topology is incomplete at {node}")
    for node, minimum in WALKING_AREA_WIDTHS.items():
        lane = root.find(f"edge[@id=':{node}_w0']/lane")
        if lane is None or float(lane.get("width")) < minimum:
            raise ValueError(f"P28 widening is incomplete at {node}")
    if math.dist(coordinate(root, TERMINUS), expected_endpoint(root)) > 0.001:
        raise ValueError("P28 endpoint does not satisfy perpendicular/half-length geometry")
    edge = root.find(f"edge[@id='{P28}']")
    if (edge.get("from"), edge.get("to")) != (P20_MIDPOINT, TERMINUS):
        raise ValueError("P28 must connect J33 to its northeast free endpoint J37")
    if root.find(f"junction[@id='{TERMINUS}']").get("type") != "dead_end":
        raise ValueError("J37 must be the one-arm terminal of P28")
    for key in set(SPLIT_ORIGINS) | NEW_EDGES:
        lane = root.find(f"edge[@id='{key}']/lane")
        if lane is None or float(lane.get("length")) <= 0:
            raise ValueError(f"Missing or non-positive pedestrian lane: {key}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--output", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--netconvert", default="netconvert")
    args = parser.parse_args()
    base_text = args.input.read_text(encoding="utf-8")
    present = {e.get("id") for e in ET.fromstring(base_text).findall("edge")}
    if P28 in present:
        validate_applied(ET.fromstring(base_text))
        print("P28 is already present; network left unchanged.")
        return
    with TemporaryDirectory(prefix="crowdsim-p28-") as directory:
        path = Path(directory) / "candidate.net.xml"
        subprocess.run([
            args.netconvert, "--sumo-net-file", str(args.input),
            "--node-files", str(SCENARIO / "patches/park_p28.nod.xml"),
            "--edge-files", str(SCENARIO / "patches/park_p28.edg.xml"),
            "--output-file", str(path), "--walkingareas", "true",
            "--offset.disable-normalization", "true", "--no-turnarounds", "true",
            "--junctions.corner-detail", "5", "--precision", "3",
        ], check=True, capture_output=True, text=True)
        result = merge_local(base_text, ET.parse(path).getroot())
        validate_applied(ET.fromstring(result))
    args.output.write_text(result, encoding="utf-8")
    print(f"P28 local patch written to {args.output}")


if __name__ == "__main__":
    main()
