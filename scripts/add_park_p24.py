"""Connect J31 to P13 on the southwest angle bisector of P22/P23.

P24 meets P13 at new J32. Rebuild the former two-arm J31 as a three-arm
pedestrian junction, retaining P22/P23 geometry at their opposite endpoints.
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
from scripts.add_park_p22_p23 import MIDPOINT, TWENTY_PERCENT, JOIN, P22, P23

SCENARIO = ROOT / "scenarios/shanghai_bund"
P13_JUNCTION = "huangpu_park_p24_p13"
P13_SOUTH = "906417852#9"
P13_NORTH = "906417852#9_p24_north"
P24 = "huangpu_park_p24"
NEW_JUNCTIONS = {P13_JUNCTION}
REBUILT_JUNCTIONS = {JOIN}
BOUNDARIES = {"8417005105", "8417005111", MIDPOINT, TWENTY_PERCENT}
JUNCTIONS = NEW_JUNCTIONS | REBUILT_JUNCTIONS | BOUNDARIES
NEW_EDGES = {P13_NORTH, P24}
SPLIT_ORIGINS = {
    P13_SOUTH: (P13_SOUTH, True), P13_NORTH: (P13_SOUTH, False),
    # Recut the J31 ends of P22/P23, preserving their validated J29/J30 ends.
    P22: (P22, True), P23: (P23, True),
}
WALKING_AREA_WIDTHS = {P13_JUNCTION: 4.0, JOIN: 4.0}
PROVENANCE = "<!-- Local P24 patch: scripts/add_park_p24.py; J31 to P13/J32 on the southwest P22/P23 angle bisector. -->"


def merge_local(base_text: str, candidate: ET.Element) -> str:
    return merge_scoped(
        base_text, candidate, new_junctions=NEW_JUNCTIONS, boundaries=BOUNDARIES,
        rebuilt_junctions=REBUILT_JUNCTIONS, new_edges=NEW_EDGES,
        split_origins=SPLIT_ORIGINS, walking_area_widths=WALKING_AREA_WIDTHS,
        provenance=PROVENANCE,
    )


def validate_applied(root: ET.Element) -> None:
    expected = {P13_JUNCTION: {P13_SOUTH, P13_NORTH, P24}, JOIN: {P22, P23, P24}}
    for node, neighbours in expected.items():
        actual = {e.get("id") for e in root.findall("edge") if node in (e.get("from"), e.get("to"))}
        lane = root.find(f"edge[@id=':{node}_w0']/lane")
        if actual != neighbours or lane is None or float(lane.get("width")) < WALKING_AREA_WIDTHS[node]:
            raise ValueError(f"P24 topology or widening is incomplete at {node}")
    edge = root.find(f"edge[@id='{P24}']")
    if edge is None or (edge.get("from"), edge.get("to")) != (JOIN, P13_JUNCTION):
        raise ValueError("P24 must connect J31 to its P13 access junction")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--output", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--netconvert", default="netconvert")
    args = parser.parse_args()
    base_text = args.input.read_text(encoding="utf-8")
    root = ET.fromstring(base_text)
    present = {e.get("id") for e in root.findall("edge")}
    if present & NEW_EDGES:
        if not NEW_EDGES.issubset(present):
            raise ValueError("network contains an incomplete P24 patch")
        validate_applied(root)
        print("P24 is already present; network left unchanged.")
        return
    with TemporaryDirectory(prefix="crowdsim-p24-") as directory:
        path = Path(directory) / "candidate.net.xml"
        subprocess.run([
            args.netconvert, "--sumo-net-file", str(args.input),
            "--node-files", str(SCENARIO / "patches/park_p24.nod.xml"),
            "--edge-files", str(SCENARIO / "patches/park_p24.edg.xml"),
            "--output-file", str(path), "--walkingareas", "true",
            "--offset.disable-normalization", "true", "--no-turnarounds", "true",
            "--junctions.corner-detail", "5", "--precision", "3",
        ], check=True, capture_output=True, text=True)
        result = merge_local(base_text, ET.parse(path).getroot())
        validate_applied(ET.fromstring(result))
    args.output.write_text(result, encoding="utf-8")
    print(f"P24 local patch written to {args.output}")


if __name__ == "__main__":
    main()
