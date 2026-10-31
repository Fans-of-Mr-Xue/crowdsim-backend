"""Run isolated SUMO pedestrian stress tests for hotspot walking areas."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

import traci


ROOT = Path(__file__).resolve().parents[1]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--network",
        type=Path,
        default=ROOT / "scenarios" / "shanghai_bund" / "bund.net.xml",
    )
    parser.add_argument(
        "--hotspot-config",
        type=Path,
        default=ROOT / "config" / "crowd_hotspots.json",
    )
    parser.add_argument("--hotspot-id", default="people_heroes_monument")
    parser.add_argument("--junction", action="append", default=[])
    parser.add_argument("--persons-per-movement", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--max-stopped-seconds", type=float, default=15.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def load_hotspot(path: Path, hotspot_id: str) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("hotspots", payload) if isinstance(payload, dict) else payload
    if isinstance(entries, dict):
        return entries[hotspot_id]
    return next(item for item in entries if item.get("id") == hotspot_id)


def collect_walking_areas(network_root: ET.Element, hotspot: dict) -> dict[str, dict]:
    scoped_edges = set(hotspot["park_access_edges"]) | set(hotspot["entry_edges"])
    connections = [
        (connection.attrib["from"], connection.attrib["to"])
        for connection in network_root.findall("./connection")
    ]
    result = {}
    for edge in network_root.findall("./edge[@function='walkingarea']"):
        edge_id = edge.attrib["id"]
        pairs = [pair for pair in connections if edge_id in pair]
        if not any(
            edge_id in pair and (pair[0] in scoped_edges or pair[1] in scoped_edges)
            for pair in pairs
        ):
            continue
        lane = edge.find("lane")
        neighbours = sorted(
            value
            for pair in pairs
            for value in pair
            if value != edge_id and not value.startswith(":")
        )
        result[edge_id] = {
            "width": float(lane.attrib["width"]),
            "length": float(lane.attrib["length"]),
            "neighbours": sorted(set(neighbours)),
        }
    return result


def edge_length(edge: ET.Element) -> float:
    lanes = edge.findall("lane")
    pedestrian_lanes = [lane for lane in lanes if "pedestrian" in lane.attrib.get("allow", "")]
    lane = pedestrian_lanes[0] if pedestrian_lanes else lanes[0]
    return float(lane.attrib["length"])


def position_near_node(edge: ET.Element, node_id: str, distance: float) -> float:
    length = edge_length(edge)
    offset = min(max(0.1, distance), max(0.1, length - 0.1))
    if edge.attrib.get("to") == node_id:
        return max(0.1, length - offset)
    if edge.attrib.get("from") == node_id:
        return offset
    raise ValueError(f"edge {edge.attrib['id']} is not incident to node {node_id}")


def build_demand(
    path: Path,
    network_root: ET.Element,
    walking_area_id: str,
    neighbours: list[str],
    persons_per_movement: int,
) -> set[str]:
    node_id = walking_area_id[1:].rsplit("_w", 1)[0]
    edges = {edge.attrib["id"]: edge for edge in network_root.findall("./edge")}
    routes = ET.Element("routes")
    ET.SubElement(
        routes,
        "vType",
        {
            "id": "junction_pedestrian",
            "vClass": "pedestrian",
            "maxSpeed": "1.30",
            "speedFactor": "1.0",
            "speedDev": "0.1",
        },
    )
    person_ids = set()
    people = []
    movement_index = 0
    for from_edge in neighbours:
        for to_edge in neighbours:
            if from_edge == to_edge:
                continue
            depart_pos = position_near_node(edges[from_edge], node_id, 8.0)
            arrival_pos = position_near_node(edges[to_edge], node_id, 8.0)
            for person_index in range(persons_per_movement):
                person_id = f"{node_id}.{movement_index:02d}.{person_index:02d}"
                person_ids.add(person_id)
                people.append(
                    (
                        person_index * 0.5,
                        person_id,
                        depart_pos,
                        from_edge,
                        to_edge,
                        arrival_pos,
                    )
                )
            movement_index += 1

    for depart, person_id, depart_pos, from_edge, to_edge, arrival_pos in sorted(people):
        person = ET.SubElement(
            routes,
            "person",
            {
                "id": person_id,
                "type": "junction_pedestrian",
                "depart": f"{depart:.1f}",
                "departPos": f"{depart_pos:.2f}",
            },
        )
        ET.SubElement(
            person,
            "walk",
            {
                "edges": f"{from_edge} {to_edge}",
                "arrivalPos": f"{arrival_pos:.2f}",
            },
        )
    ET.ElementTree(routes).write(path, encoding="utf-8", xml_declaration=True)
    return person_ids


def test_junction(
    network_path: Path,
    network_root: ET.Element,
    walking_area_id: str,
    metadata: dict,
    temporary_directory: Path,
    persons_per_movement: int,
    timeout_seconds: float,
    max_stopped_seconds: float,
    demand_builder=build_demand,
) -> dict:
    safe_name = walking_area_id.replace(":", "").replace("#", "_")
    demand_path = temporary_directory / f"{safe_name}.rou.xml"
    log_path = temporary_directory / f"{safe_name}.sumo.log"
    planned = demand_builder(
        demand_path,
        network_root,
        walking_area_id,
        metadata["neighbours"],
        persons_per_movement,
    )
    label = f"junction-{safe_name}"
    command = [
        "sumo",
        "--net-file", str(network_path),
        "--route-files", str(demand_path),
        "--begin", "0",
        "--end", str(timeout_seconds),
        "--step-length", "0.2",
        "--ignore-route-errors", "false",
        "--time-to-teleport", "-1",
        "--pedestrian.model", "striping",
        "--pedestrian.striping.stripe-width", "0.55",
        "--pedestrian.striping.reserve-oncoming", "0.5",
        "--pedestrian.striping.jamtime", "3601",
        "--pedestrian.striping.jamtime.crossing", "3601",
        "--pedestrian.striping.jamtime.narrow", "3601",
        "--seed", "20260908",
        "--no-step-log", "true",
        "--duration-log.disable", "true",
        "--log", str(log_path),
    ]
    connection = None
    departed = set()
    arrived = set()
    stopped = {}
    max_stopped = {}
    max_walking_area_count = 0
    final_active_by_edge = {}
    error = None
    end_time = 0.0
    try:
        traci.start(command, label=label)
        connection = traci.getConnection(label)
        while connection.simulation.getTime() < timeout_seconds:
            connection.simulationStep()
            end_time = connection.simulation.getTime()
            departed.update(connection.simulation.getDepartedPersonIDList())
            arrived.update(connection.simulation.getArrivedPersonIDList())
            walking_area_count = 0
            for person_id in connection.person.getIDList():
                speed = connection.person.getSpeed(person_id)
                edge_id = connection.person.getRoadID(person_id)
                if edge_id == walking_area_id:
                    walking_area_count += 1
                if speed < 0.05:
                    stopped[person_id] = stopped.get(person_id, 0.0) + 0.2
                    max_stopped[person_id] = max(max_stopped.get(person_id, 0.0), stopped[person_id])
                else:
                    stopped[person_id] = 0.0
            max_walking_area_count = max(max_walking_area_count, walking_area_count)
            if connection.simulation.getMinExpectedNumber() == 0:
                break
        for person_id in connection.person.getIDList():
            edge_id = connection.person.getRoadID(person_id)
            final_active_by_edge[edge_id] = final_active_by_edge.get(edge_id, 0) + 1
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if connection is not None:
            connection.close()
    longest_stop = max(max_stopped.values(), default=0.0)
    passed = (
        error is None
        and departed == planned
        and arrived == planned
        and longest_stop <= max_stopped_seconds
        and end_time < timeout_seconds
    )
    return {
        "walking_area_id": walking_area_id,
        "width_m": metadata["width"],
        "neighbours": metadata["neighbours"],
        "movement_count": len(metadata["neighbours"]) * (len(metadata["neighbours"]) - 1),
        "planned": len(planned),
        "departed": len(departed),
        "arrived": len(arrived),
        "end_time_seconds": round(end_time, 1),
        "max_continuous_stop_seconds": round(longest_stop, 1),
        "max_walking_area_count": max_walking_area_count,
        "final_active_by_edge": final_active_by_edge,
        "error": error,
        "passed": passed,
    }


def main() -> int:
    args = parse_args()
    if args.persons_per_movement <= 0:
        raise ValueError("persons-per-movement must be positive")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise ValueError("timeout-seconds must be positive and finite")
    if not math.isfinite(args.max_stopped_seconds) or args.max_stopped_seconds < 0:
        raise ValueError("max-stopped-seconds must be finite and non-negative")

    network_path = args.network.resolve()
    network_root = ET.parse(network_path).getroot()
    hotspot = load_hotspot(args.hotspot_config.resolve(), args.hotspot_id)
    walking_areas = collect_walking_areas(network_root, hotspot)
    selected = args.junction or sorted(walking_areas)
    unknown = sorted(set(selected) - set(walking_areas))
    if unknown:
        raise ValueError(f"junctions are outside the hotspot walking-area scope: {unknown}")

    with TemporaryDirectory(prefix="crowdsim-junction-tests-") as directory:
        temporary_directory = Path(directory)
        results = []
        for index, walking_area_id in enumerate(selected, 1):
            print(
                f"[{index}/{len(selected)}] testing {walking_area_id}",
                file=sys.stderr,
                flush=True,
            )
            results.append(test_junction(
                network_path,
                network_root,
                walking_area_id,
                walking_areas[walking_area_id],
                temporary_directory,
                args.persons_per_movement,
                args.timeout_seconds,
                args.max_stopped_seconds,
            ))

    report = {
        "test_type": "isolated_sumo_hotspot_walking_area",
        "network": str(network_path),
        "hotspot_id": args.hotspot_id,
        "jamtime_seconds": 3601,
        "junction_count": len(results),
        "passed_count": sum(item["passed"] for item in results),
        "failed_count": sum(not item["passed"] for item in results),
        "results": results,
    }
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if report["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
