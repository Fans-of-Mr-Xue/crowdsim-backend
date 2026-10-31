"""Add J29 to the J28-J15 midpoint and J21 to the near-J18 trisection of P17b.

P25/P26 are straight pedestrian footways. Rebuild J29 (four arms) and J21
(five arms); preserve all outer junction polygons and far-end lane cuts.
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
from scripts.add_park_p21 import merge_local as merge_scoped, EAST, P20_SOUTH
from scripts.add_park_p22_p23 import MIDPOINT, TWENTY_PERCENT, JOIN, P22, P23

SCENARIO = ROOT / "scenarios/shanghai_bund"
P20_MIDPOINT = "huangpu_park_p25_p20"
P17_TRISECTION = "huangpu_park_p26_p17"
J21 = "8417005102"
P20_LAST = "huangpu_park_j05_j15_p21_south_p25_south"
P17_MIDDLE = "906417854_p23_south"
P17_LAST = "906417854_p23_south_p26_south"
P25 = "huangpu_park_p25"
P26 = "huangpu_park_p26"
NEW_JUNCTIONS = {P20_MIDPOINT, P17_TRISECTION}
REBUILT_JUNCTIONS = {MIDPOINT, J21}
BOUNDARIES = {EAST, "8417005113", TWENTY_PERCENT, "8417005115",
              "8417005111", JOIN, "8417005101", "8417005103", "8417005105"}
JUNCTIONS = NEW_JUNCTIONS | REBUILT_JUNCTIONS | BOUNDARIES
NEW_EDGES = {P20_LAST, P17_LAST, P25, P26}
SPLIT_ORIGINS = {
    P20_SOUTH: (P20_SOUTH, True), P20_LAST: (P20_SOUTH, False),
    P17_MIDDLE: (P17_MIDDLE, True), P17_LAST: (P17_MIDDLE, False),
    "906417853#0": ("906417853#0", True),
    "906417853#0_p22_east": ("906417853#0_p22_east", False),
    P22: (P22, False),
    "906417852#5": ("906417852#5", True),
    "906417852#6": ("906417852#6", False),
    "906417855#1": ("906417855#1", True),
    "906417855#2": ("906417855#2", False),
}
WALKING_AREA_WIDTHS = {node: 4.0 for node in NEW_JUNCTIONS | REBUILT_JUNCTIONS}
PROVENANCE = "<!-- Local P25/P26 patch: scripts/add_park_p25_p26.py; J29 to J28-J15 midpoint and J21 to near-J18 P17b trisection. -->"


def merge_local(base_text: str, candidate: ET.Element) -> str:
    return merge_scoped(
        base_text, candidate, new_junctions=NEW_JUNCTIONS, boundaries=BOUNDARIES,
        rebuilt_junctions=REBUILT_JUNCTIONS, new_edges=NEW_EDGES,
        split_origins=SPLIT_ORIGINS, walking_area_widths=WALKING_AREA_WIDTHS,
        provenance=PROVENANCE,
    )


def validate_applied(root: ET.Element) -> None:
    expected = {
        P20_MIDPOINT: {P20_SOUTH, P20_LAST, P25},
        P17_TRISECTION: {P17_MIDDLE, P17_LAST, P26},
        MIDPOINT: {"906417853#0", "906417853#0_p22_east", P22, P25},
        J21: {"906417852#5", "906417852#6", "906417855#1", "906417855#2", P26},
    }
    extension = root.find("edge[@id='huangpu_park_p27_a']")
    if extension is not None and extension.get("from") == P17_TRISECTION:
        expected[P17_TRISECTION].add("huangpu_park_p27_a")
    extension = root.find("edge[@id='huangpu_park_p28']")
    if extension is not None and extension.get("from") == P20_MIDPOINT:
        expected[P20_MIDPOINT].add("huangpu_park_p28")
    for node, neighbours in expected.items():
        actual = {e.get("id") for e in root.findall("edge") if node in (e.get("from"), e.get("to"))}
        lane = root.find(f"edge[@id=':{node}_w0']/lane")
        if actual != neighbours or lane is None or float(lane.get("width")) < WALKING_AREA_WIDTHS[node]:
            raise ValueError(f"P25/P26 topology or widening is incomplete at {node}")
    for key, start, end in ((P25, MIDPOINT, P20_MIDPOINT), (P26, J21, P17_TRISECTION)):
        edge = root.find(f"edge[@id='{key}']")
        if edge is None or (edge.get("from"), edge.get("to")) != (start, end):
            raise ValueError(f"Incorrect endpoints for {key}")
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
    if present & NEW_EDGES:
        if not NEW_EDGES.issubset(present):
            raise ValueError("network contains an incomplete P25/P26 patch")
        validate_applied(ET.fromstring(base_text))
        print("P25/P26 are already present; network left unchanged.")
        return
    with TemporaryDirectory(prefix="crowdsim-p25-p26-") as directory:
        path = Path(directory) / "candidate.net.xml"
        subprocess.run([
            args.netconvert, "--sumo-net-file", str(args.input),
            "--node-files", str(SCENARIO / "patches/park_p25_p26.nod.xml"),
            "--edge-files", str(SCENARIO / "patches/park_p25_p26.edg.xml"),
            "--output-file", str(path), "--walkingareas", "true",
            "--offset.disable-normalization", "true", "--no-turnarounds", "true",
            "--junctions.corner-detail", "5", "--precision", "3",
        ], check=True, capture_output=True, text=True)
        result = merge_local(base_text, ET.parse(path).getroot())
        validate_applied(ET.fromstring(result))
    args.output.write_text(result, encoding="utf-8")
    print(f"P25/P26 local patch written to {args.output}")


if __name__ == "__main__":
    main()
