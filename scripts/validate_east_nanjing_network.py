"""Validate all real pedestrian junctions and every ordered road pair in SUMO."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

import traci
import traci.constants as tc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_east_nanjing_network import SCENE, demand_xml, digest, route_cases, write_xml
from scripts.validate_hotspot_junctions import collect_walking_areas, test_junction


def configure_proj_data() -> None:
    """Find the installed SUMO bundle's own database when its launcher omitted it."""
    if os.environ.get("PROJ_DATA") or os.environ.get("PROJ_LIB"):
        return
    binary = shutil.which("sumo")
    if not binary:
        return
    prefix = Path(binary).resolve().parent.parent
    candidates = [prefix / "share/proj/proj.db"]
    candidates.extend(prefix.glob("framework/*/Versions/*/*/share/proj/proj.db"))
    for path in candidates:
        if path.is_file():
            os.environ["PROJ_DATA"] = str(path.parent)
            return


def whole_network(network: Path, temporary: Path) -> dict:
    cases = route_cases(network, all_pairs=True)
    demand = temporary / "all-pairs.rou.xml"
    write_xml(demand, demand_xml(cases))
    planned = {case["id"] for case in cases}
    log = temporary / "all-pairs.log"
    command = ["sumo", "--net-file", str(network), "--route-files", str(demand),
               "--end", "2400", "--step-length", "0.5", "--ignore-route-errors", "false",
               "--time-to-teleport", "-1", "--pedestrian.model", "striping",
               "--pedestrian.striping.stripe-width", "0.55", "--pedestrian.striping.reserve-oncoming", "0.5",
               "--pedestrian.striping.jamtime", "3601", "--pedestrian.striping.jamtime.crossing", "3601",
               "--pedestrian.striping.jamtime.narrow", "3601", "--seed", "20260908",
               "--no-step-log", "true", "--duration-log.disable", "true", "--log", str(log)]
    label = "nanjing-network-matrix"
    traci.start(command, label=label)
    connection = traci.getConnection(label)
    departed, arrived = set(), set()
    stopped, maxima = {}, {}
    traversed = set()
    last_progress = -60.0
    end_time = 0.0
    try:
        from crowdsim.infrastructure.network_adapter import ResearchNetwork
        model = ResearchNetwork(str(network))
        xy = model.net.getNode("J11").getCoord()
        expected_lonlat = model.xy_to_lonlat(*xy)
        actual_lonlat = connection.simulation.convertGeo(*xy)
        projection_error = max(abs(a-b) for a, b in zip(expected_lonlat, actual_lonlat))
        while connection.simulation.getTime() < 2400:
            connection.simulationStep()
            end_time = connection.simulation.getTime()
            new = set(connection.simulation.getDepartedPersonIDList())
            completed = set(connection.simulation.getArrivedPersonIDList())
            departed.update(new)
            arrived.update(completed)
            for person in new - completed:
                connection.person.subscribe(person, [tc.VAR_SPEED, tc.VAR_ROAD_ID])
            for person, values in connection.person.getAllSubscriptionResults().items():
                traversed.add(values[tc.VAR_ROAD_ID])
                if values[tc.VAR_SPEED] < .05:
                    stopped[person] = stopped.get(person, 0.0) + .5
                    maxima[person] = max(maxima.get(person, 0.0), stopped[person])
                else:
                    stopped[person] = 0.0
            if end_time - last_progress >= 120:
                print(f"matrix: time={end_time:.0f}s departed={len(departed)} arrived={len(arrived)}", file=sys.stderr, flush=True)
                last_progress = end_time
            if connection.simulation.getMinExpectedNumber() == 0:
                break
        active = {person: connection.person.getRoadID(person) for person in connection.person.getIDList()}
    finally:
        connection.close()
    longest_stop = max(maxima.values(), default=0.0)
    log_text = log.read_text(encoding="utf-8")
    normal = {edge.get("id") for edge in ET.parse(network).getroot().findall("edge")
              if edge.get("function", "normal") == "normal"}
    return {"type": "all_ordered_road_pairs_plus_bidirectional_edges_and_forced_connections",
            "planned": len(planned), "departed": len(departed), "arrived": len(arrived),
            "ordered_road_pairs": len(normal) * (len(normal) - 1),
            "additional_directional_cases": len(cases) - len(normal) * (len(normal) - 1),
            "end_time_seconds": end_time, "max_continuous_stop_seconds": longest_stop,
            "roads_observed": sorted(traversed & normal), "missing_observed_roads": sorted(normal - traversed),
            "remaining_people": active, "jam_resolution_warning": "jammed" in log_text.lower(),
            "traci_projection_error_degrees": projection_error,
            "log": log_text,
            "passed": planned == departed == arrived and not active and end_time < 2400
                      and longest_stop <= 120 and not (normal - traversed) and "jammed" not in log_text.lower()
                      and projection_error < 1e-7}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", type=Path, default=SCENE / "east_nanjing.net.xml")
    parser.add_argument("--output", type=Path, default=SCENE / "validation_report.json")
    args = parser.parse_args()
    configure_proj_data()
    network = args.network.resolve()
    root = ET.parse(network).getroot()
    normal = [edge.get("id") for edge in root.findall("edge") if edge.get("function", "normal") == "normal"]
    areas = collect_walking_areas(root, {"park_access_edges": normal, "entry_edges": []})
    results = []
    with TemporaryDirectory(prefix="crowdsim-nanjing-validation-") as folder:
        temporary = Path(folder)
        for index, (identifier, metadata) in enumerate(sorted(areas.items()), 1):
            print(f"[{index}/{len(areas)}] {identifier}, width={metadata['width']}m", file=sys.stderr, flush=True)
            results.append(test_junction(network, root, identifier, metadata, temporary, 4, 180.0, 15.0))
        matrix = whole_network(network, temporary)
    report = {"network": network.name, "network_sha256": digest(network),
              "sumo_version": subprocess.run(["sumo", "--version"], capture_output=True, text=True).stdout.splitlines()[0],
              "settings": {"stripe_width_m": .55, "reserve_oncoming": .5, "jamtime_seconds": 3601,
                           "local_step_seconds": .2, "whole_network_step_seconds": .5,
                           "persons_per_ordered_junction_movement": 4, "seed": 20260908},
              "isolated_junctions": {"count": len(results), "passed_count": sum(r["passed"] for r in results),
                                     "planned_people": sum(r["planned"] for r in results),
                                     "max_continuous_stop_seconds": max(r["max_continuous_stop_seconds"] for r in results),
                                     "results": results},
              "whole_network": matrix, "passed": all(r["passed"] for r in results) and matrix["passed"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "passed": report["passed"],
                      "junctions": report["isolated_junctions"]["passed_count"],
                      "full_network_people": matrix["arrived"], "max_stopped_seconds": matrix["max_continuous_stop_seconds"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
