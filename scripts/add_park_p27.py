"""Add a three-segment J34-to-J30 trapezoid footway, preserving the P17 bend.

The J34-J30 chord is the southwest long base. The northeast short base is half
as long; equal legs are symmetric about the perpendicular bisector. The J34
leg is parallel and codirectional with J21-to-J34 (P26), fixing the height.
The existing bent P17b remains unchanged as the approximate fourth side.
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
from scripts.add_park_p21 import merge_local as merge_scoped
from scripts.add_park_p22_p23 import TWENTY_PERCENT, JOIN, P17_NORTH, P23
from scripts.add_park_p25_p26 import J21, P17_TRISECTION, P17_MIDDLE, P17_LAST, P26

SCENARIO = ROOT / "scenarios/shanghai_bund"
SOUTHEAST_CORNER = "huangpu_park_p27_se"
NORTHEAST_CORNER = "huangpu_park_p27_ne"
P27_A = "huangpu_park_p27_a"
P27_B = "huangpu_park_p27_b"
P27_C = "huangpu_park_p27_c"
NEW_EDGES = {P27_A, P27_B, P27_C}
NEW_JUNCTIONS = {SOUTHEAST_CORNER, NORTHEAST_CORNER}
REBUILT_JUNCTIONS = {TWENTY_PERCENT, P17_TRISECTION}
BOUNDARIES = {"8417005113", JOIN, "8417005115", J21}
JUNCTIONS = NEW_JUNCTIONS | REBUILT_JUNCTIONS | BOUNDARIES
SPLIT_ORIGINS = {
    P17_NORTH: (P17_NORTH, True), P23: (P23, False),
    P17_LAST: (P17_LAST, False), P26: (P26, True),
}
WALKING_AREA_WIDTHS = {node: 4.0 for node in NEW_JUNCTIONS | REBUILT_JUNCTIONS}
# Four-arm J34 stalled at 4m; 5m passed the all-movements stress test.
WALKING_AREA_WIDTHS[P17_TRISECTION] = 5.0
PROVENANCE = "<!-- Local P27 patch: scripts/add_park_p27.py; three-segment northeast trapezoid branch, half-length short base, southeast leg parallel to P26, original P17b bend retained. -->"


def coordinate(root: ET.Element, node: str) -> tuple[float, float]:
    junction = root.find(f"junction[@id='{node}']")
    return float(junction.get("x")), float(junction.get("y"))


def expected_corners(root: ET.Element) -> tuple[tuple[float, float], tuple[float, float]]:
    a, b, origin = (coordinate(root, n) for n in (P17_TRISECTION, TWENTY_PERCENT, J21))
    length = math.dist(a, b)
    unit = tuple((b[i] - a[i]) / length for i in (0, 1))
    northeast = (unit[1], -unit[0])
    direction = tuple(a[i] - origin[i] for i in (0, 1))
    projection = sum(direction[i] * unit[i] for i in (0, 1))
    if projection <= 0:
        raise ValueError("P26 direction cannot produce the requested codirectional trapezoid leg")
    scale = length / 4 / projection
    height = scale * sum(direction[i] * northeast[i] for i in (0, 1))
    if height <= 0:
        raise ValueError("Trapezoid short base must lie northeast of the long base")
    first = tuple(a[i] + scale * direction[i] for i in (0, 1))
    second = tuple(b[i] - length / 4 * unit[i] + height * northeast[i] for i in (0, 1))
    return first, second


def merge_local(base_text: str, candidate: ET.Element) -> str:
    return merge_scoped(
        base_text, candidate, new_junctions=NEW_JUNCTIONS, boundaries=BOUNDARIES,
        rebuilt_junctions=REBUILT_JUNCTIONS, rebuilt_edges={P17_MIDDLE},
        new_edges=NEW_EDGES, split_origins=SPLIT_ORIGINS,
        walking_area_widths=WALKING_AREA_WIDTHS, provenance=PROVENANCE,
    )


def validate_applied(root: ET.Element) -> None:
    expected = {
        P17_TRISECTION: {P17_MIDDLE, P17_LAST, P26, P27_A},
        TWENTY_PERCENT: {P17_NORTH, P17_MIDDLE, P23, P27_C},
        SOUTHEAST_CORNER: {P27_A, P27_B},
        NORTHEAST_CORNER: {P27_B, P27_C},
    }
    for node, neighbours in expected.items():
        actual = {e.get("id") for e in root.findall("edge") if node in (e.get("from"), e.get("to"))}
        lane = root.find(f"edge[@id=':{node}_w0']/lane")
        if actual != neighbours or lane is None or float(lane.get("width")) < WALKING_AREA_WIDTHS[node]:
            raise ValueError(f"P27 topology or widening is incomplete at {node}")
    for node, coord in zip((SOUTHEAST_CORNER, NORTHEAST_CORNER), expected_corners(root)):
        if math.dist(coordinate(root, node), coord) > 0.001:
            raise ValueError(f"Incorrect P27 trapezoid corner: {node}")
    for key, start, end in ((P27_A, P17_TRISECTION, SOUTHEAST_CORNER),
                            (P27_B, SOUTHEAST_CORNER, NORTHEAST_CORNER),
                            (P27_C, NORTHEAST_CORNER, TWENTY_PERCENT)):
        edge = root.find(f"edge[@id='{key}']")
        if edge is None or (edge.get("from"), edge.get("to")) != (start, end):
            raise ValueError(f"Incorrect endpoints for {key}")
    for key in set(SPLIT_ORIGINS) | NEW_EDGES | {P17_MIDDLE}:
        lane = root.find(f"edge[@id='{key}']/lane")
        if lane is None or float(lane.get("length")) <= 0:
            raise ValueError(f"Missing or non-positive pedestrian lane: {key}")
    bend = "18570.170,5792.320"
    if bend not in root.find(f"edge[@id='{P17_MIDDLE}']").get("shape", "").split():
        raise ValueError("P27 must preserve the original P17b centerline bend")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--output", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--netconvert", default="netconvert")
    args = parser.parse_args()
    base_text = args.input.read_text(encoding="utf-8")
    present = {e.get("id") for e in ET.fromstring(base_text).findall("edge")}
    if present & NEW_EDGES:
        if not NEW_EDGES.issubset(present):
            raise ValueError("network contains an incomplete P27 patch")
        validate_applied(ET.fromstring(base_text))
        print("P27 is already present; network left unchanged.")
        return
    with TemporaryDirectory(prefix="crowdsim-p27-") as directory:
        path = Path(directory) / "candidate.net.xml"
        subprocess.run([
            args.netconvert, "--sumo-net-file", str(args.input),
            "--node-files", str(SCENARIO / "patches/park_p27.nod.xml"),
            "--edge-files", str(SCENARIO / "patches/park_p27.edg.xml"),
            "--output-file", str(path), "--walkingareas", "true",
            "--offset.disable-normalization", "true", "--no-turnarounds", "true",
            "--junctions.corner-detail", "5", "--precision", "3",
        ], check=True, capture_output=True, text=True)
        result = merge_local(base_text, ET.parse(path).getroot())
        validate_applied(ET.fromstring(result))
    args.output.write_text(result, encoding="utf-8")
    print(f"P27 local patch written to {args.output}")


if __name__ == "__main__":
    main()
