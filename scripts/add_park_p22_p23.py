"""Add two terminating footways P22/P23 between P15 and P17.

J29 is the midpoint of the complete J17-J15 centerline. J30 is 20% of
the complete P17 polyline from J15. P22 follows P17's first straight segment
southeast, while P23 follows P15 southwest. Both end at the same J31.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.add_park_p21 import merge_local as merge_scoped

SCENARIO = ROOT / "scenarios/shanghai_bund"
MIDPOINT = "huangpu_park_p22_start"
TWENTY_PERCENT = "huangpu_park_p23_start"
JOIN = "huangpu_park_p22_p23_join"
P15_WEST = "906417853#0"
P15_EAST = "906417853#0_p22_east"
P17_NORTH = "906417854"
P17_SOUTH = "906417854_p23_south"
P22 = "huangpu_park_p22"
P23 = "huangpu_park_p23"
NEW_JUNCTIONS = {MIDPOINT, TWENTY_PERCENT, JOIN}
BOUNDARIES = {"8417005111", "8417005113", "8417005115"}
JUNCTIONS = NEW_JUNCTIONS | BOUNDARIES
NEW_EDGES = {P15_EAST, P17_SOUTH, P22, P23}
SPLIT_ORIGINS = {
    P15_WEST: (P15_WEST, True), P15_EAST: (P15_WEST, False),
    P17_NORTH: (P17_NORTH, True), P17_SOUTH: (P17_NORTH, False),
}
WALKING_AREA_WIDTHS = {MIDPOINT: 4.0, TWENTY_PERCENT: 4.0, JOIN: 4.0, "8417005113": 6.0}
PROVENANCE = "<!-- Local P22/P23 patch: scripts/add_park_p22_p23.py; P15 midpoint and P17 20% from J15, parallel to the near-J15 P17 segment and P15; both terminate at J31. -->"


def merge_local(base_text: str, candidate: ET.Element) -> str:
    return merge_scoped(
        base_text, candidate, new_junctions=NEW_JUNCTIONS, boundaries=BOUNDARIES,
        new_edges=NEW_EDGES, split_origins=SPLIT_ORIGINS,
        walking_area_widths=WALKING_AREA_WIDTHS, provenance=PROVENANCE,
    )


def validate_applied(root: ET.Element) -> None:
    edges = {edge.get("id"): edge for edge in root.findall("edge")}
    expected = {
        MIDPOINT: {P15_WEST, P15_EAST, P22},
        TWENTY_PERCENT: {P17_NORTH, P17_SOUTH, P23}, JOIN: {P22, P23},
    }
    # P24 is a later extension from the shared endpoint; the original two
    # footways still terminate at J31 and must remain valid after that patch.
    extension = edges.get("huangpu_park_p24")
    if extension is not None and extension.get("from") == JOIN:
        expected[JOIN].add("huangpu_park_p24")
    extension = edges.get("huangpu_park_p25")
    if extension is not None and extension.get("from") == MIDPOINT:
        expected[MIDPOINT].add("huangpu_park_p25")
    extension = edges.get("huangpu_park_p27_c")
    if extension is not None and extension.get("to") == TWENTY_PERCENT:
        expected[TWENTY_PERCENT].add("huangpu_park_p27_c")
    for node, incident in expected.items():
        actual = {key for key, edge in edges.items() if node in (edge.get("from"), edge.get("to"))}
        lane = edges.get(f":{node}_w0")
        if actual != incident or lane is None or float(lane.find("lane").get("width")) < WALKING_AREA_WIDTHS[node]:
            raise ValueError(f"P22/P23 topology or widening is incomplete at {node}")
    if any(edges[key].get("to") != JOIN for key in (P22, P23)):
        raise ValueError("P22 and P23 must both terminate at their common endpoint")
    for node, minimum in WALKING_AREA_WIDTHS.items():
        lane = root.find(f"edge[@id=':{node}_w0']/lane")
        if lane is None or float(lane.get("width")) < minimum:
            raise ValueError(f"P22/P23 widening is incomplete at {node}")


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
            raise ValueError("network contains an incomplete P22/P23 patch")
        validate_applied(root)
        print("P22/P23 are already present; network left unchanged.")
        return
    with TemporaryDirectory(prefix="crowdsim-p22-p23-") as directory:
        path = Path(directory) / "candidate.net.xml"
        subprocess.run([
            args.netconvert, "--sumo-net-file", str(args.input),
            "--node-files", str(SCENARIO / "patches/park_p22_p23.nod.xml"),
            "--edge-files", str(SCENARIO / "patches/park_p22_p23.edg.xml"),
            "--output-file", str(path), "--walkingareas", "true",
            "--offset.disable-normalization", "true", "--no-turnarounds", "true",
            "--junctions.corner-detail", "5", "--precision", "3",
        ], check=True, capture_output=True, text=True)
        result = merge_local(base_text, ET.parse(path).getroot())
        validate_applied(ET.fromstring(result))
    args.output.write_text(result, encoding="utf-8")
    print(f"P22/P23 local patch written to {args.output}")


if __name__ == "__main__":
    main()
