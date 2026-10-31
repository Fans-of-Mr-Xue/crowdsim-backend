"""Verify mixed pedestrian journeys into P28's J37 dead end and back through J33."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.add_park_p28 import P28, P20_MIDPOINT, P20_SOUTH, P20_LAST, P25
from scripts.validate_hotspot_junctions import position_near_node, test_junction


def terminal_demand(path, root, area, neighbours, persons_per_movement):
    edges = {e.get("id"): e for e in root.findall("edge")}
    routes = ET.Element("routes")
    ET.SubElement(routes, "vType", id="junction_pedestrian", vClass="pedestrian",
                  maxSpeed="1.30", speedFactor="1.0", speedDev="0.1")
    endpoint_position = float(edges[P28].find("lane").get("length")) - 0.1
    identifiers = set()
    for index in range(persons_per_movement):
        for incoming in neighbours:
            for outgoing in neighbours:
                identifier = f"terminal.{incoming}.{outgoing}.{index}"
                identifiers.add(identifier)
                person = ET.SubElement(routes, "person", id=identifier, type="junction_pedestrian",
                                       depart=f"{index * .5:.1f}",
                                       departPos=f"{position_near_node(edges[incoming], P20_MIDPOINT, 8):.3f}")
                # Complete the first walk near J37, then reverse along P28.
                ET.SubElement(person, "walk", edges=f"{incoming} {P28}", arrivalPos=f"{endpoint_position:.3f}")
                ET.SubElement(person, "walk", edges=f"{P28} {outgoing}",
                              arrivalPos=f"{position_near_node(edges[outgoing], P20_MIDPOINT, 8):.3f}")
    ET.ElementTree(routes).write(path, encoding="utf-8", xml_declaration=True)
    return identifiers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", type=Path, default=ROOT / "scenarios/shanghai_bund/bund.net.xml")
    parser.add_argument("--output", type=Path, default=ROOT / "runs/park_p28_terminal_checks.json")
    args = parser.parse_args()
    root = ET.parse(args.network).getroot()
    area = f":{P20_MIDPOINT}_w0"
    neighbours = sorted((P20_SOUTH, P20_LAST, P25))
    lane = root.find(f"edge[@id='{area}']/lane")
    with TemporaryDirectory(prefix="crowdsim-p28-terminal-") as directory:
        result = test_junction(args.network.resolve(), root, area,
                               {"neighbours": neighbours, "width": float(lane.get("width"))},
                               Path(directory), 2, 180, 15, demand_builder=terminal_demand)
    result["movement_count"] = 9  # All three incoming/outgoing pairs, including return to origin.
    report = {"test_type": "isolated_p28_terminal_roundtrip", "network": str(args.network.resolve()),
              "route": "approach -> P28 near J37 -> P28 reversed -> approach",
              "jamtime_seconds": 3601, "persons_per_approach_pair": 2, "result": result}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
