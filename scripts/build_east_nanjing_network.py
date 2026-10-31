"""Build the approved Nanjing Road pedestrian graph and preserve width rules.

The independent scene uses the diagram's R/J IDs and the Bund's metre CRS.
netconvert creates real pedestrian connections; explicit postprocessing keeps
walking areas at least 4m, and four-arm areas at least 5m, on every rebuild.
It never modifies the source Bund network or the frontend preset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

import sumolib

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "scenarios/east_nanjing_road"
sys.path.insert(0, str(ROOT))


def route_cases(network_path: Path, *, all_pairs: bool = False) -> list[dict]:
    from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
    from crowdsim.infrastructure.network_adapter import ResearchNetwork
    network = ResearchNetwork(str(network_path))
    router = PositionAwarePedestrianRouter(network)
    normal = sorted(key for key in router.walkable_edges if not key.startswith(":"))
    cases = []

    def add(identifier, start_edge, start_fraction, end_edge, end_fraction, via=(), orientations=()):
        start = EdgePosition(start_edge, network.edges[start_edge].getLength() * start_fraction)
        target = EdgePosition(end_edge, network.edges[end_edge].getLength() * end_fraction)
        route = (router.route_via_edges(start, target, via, via_orientations=orientations)
                 if via else router.route(start, target))
        cases.append({"id": identifier, "edges": route.edges, "depart_pos": start.offset_m,
                      "arrival_pos": target.offset_m, "distance_m": route.distance_m})

    for road in normal:
        add(f"edge-{road}-forward", road, .1, road, .9)
        add(f"edge-{road}-reverse", road, .9, road, .1)
    for start, end, direction in [("R25c", "R26c", 0), ("R26c", "R25c", 1)]:
        add(f"south-{direction}", start, .1, end, .1, ("R31",), (direction,))
    for branch in ["R08b", "R09a"]:
        add(f"middle-to-{branch}", "R02", .5, branch, .5, ("R07",), (1,))
        add(f"{branch}-to-middle", branch, .5, "R02", .5, ("R07",), (0,))
    if all_pairs:
        for first in normal:
            for last in normal:
                if first != last:
                    add(f"pair-{first}-to-{last}", first, .5, last, .5)
    return cases


def demand_xml(cases: list[dict], *, departure_interval: float = 1.0) -> ET.Element:
    root = ET.Element("routes")
    ET.SubElement(root, "vType", id="network_test_pedestrian", vClass="pedestrian",
                  maxSpeed="1.30", speedFactor="1.0", speedDev="0.1")
    for index, case in enumerate(cases):
        person = ET.SubElement(root, "person", id=case["id"], type="network_test_pedestrian",
                               depart=f"{index * departure_interval:.2f}", departPos=f'{case["depart_pos"]:.8f}')
        ET.SubElement(person, "walk", edges=" ".join(case["edges"]), arrivalPos=f'{case["arrival_pos"]:.8f}')
    return root


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_xml(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="    ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def validate_spec(spec: dict) -> dict[str, list[str]]:
    nodes = {n["id"]: n for n in spec["nodes"]}
    roads = {r["id"]: r for r in spec["roads"]}
    if len(nodes) != len(spec["nodes"]) or len(roads) != len(spec["roads"]):
        raise ValueError("Duplicate diagram IDs")
    adjacency = {key: [] for key in nodes}
    for road in roads.values():
        for key, endpoint in [("from", road["xy_m"][0]), ("to", road["xy_m"][-1])]:
            node = nodes[road[key]]
            if math.dist(endpoint, node["xy_m"]) > 1e-7:
                raise ValueError(f"Road/node endpoint mismatch: {road['id']}")
            adjacency[node["id"]].append(road["id"])
    visited, stack = set(), [next(iter(nodes))]
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        for key in adjacency[node]:
            stack.extend([roads[key]["from"], roads[key]["to"]])
    if visited != set(nodes):
        raise ValueError("Diagram must be connected")
    return adjacency


def build(spec_path: Path = SCENE / "network_spec.json", output: Path = SCENE,
          netconvert: str = "netconvert") -> dict:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    incident = validate_spec(spec)
    source_path = (spec_path.parent / spec["base_network"]).resolve()
    source_hash = digest(source_path)
    source_root = ET.parse(source_path).getroot()
    source_net = sumolib.net.readNet(str(source_path), withInternal=True,
                                    withPedestrianConnections=True)
    policy = spec["width_policy"]
    output.mkdir(parents=True, exist_ok=True)
    if output.resolve() == source_path.parent:
        raise ValueError("The new scene must not overwrite the original Bund scene")
    nodes = ET.Element("nodes")
    for node in spec["nodes"]:
        ET.SubElement(nodes, "node", id=node["id"], x=f'{node["xy_m"][0]:.8f}',
                      y=f'{node["xy_m"][1]:.8f}', type="priority")
    edges = ET.Element("edges")
    road_metadata = []
    for road in spec["roads"]:
        width = policy["new_road_lane_m"]
        width_source = "new pedestrian road; same 2m width as connected footways"
        if road["source_edge_ids"]:
            source = source_net.getEdge(road["source_edge_ids"][0])
            lanes = [lane for lane in source.getLanes() if lane.allows("pedestrian")]
            if not lanes:
                raise ValueError(f"Source road forbids pedestrians: {road['id']}")
            width = lanes[0].getWidth()
            width_source = lanes[0].getID()
        edge = ET.SubElement(edges, "edge", {
            "id": road["id"], "from": road["from"], "to": road["to"],
            "numLanes": "1", "allow": "pedestrian", "speed": "1.4",
            "width": f"{width:.8f}", "spreadType": "center",
            "shape": " ".join(f"{x:.8f},{y:.8f}" for x, y in road["xy_m"]),
        })
        ET.SubElement(edge, "param", key="diagram.road", value=road["id"])
        ET.SubElement(edge, "param", key="source.edges", value=" ".join(road["source_edge_ids"]))
        road_metadata.append({"id": road["id"], "from": road["from"], "to": road["to"],
                              "lane_width_m": width, "width_source": width_source,
                              "source_edge_ids": road["source_edge_ids"]})
    node_path, edge_path = output / "network.nod.xml", output / "network.edg.xml"
    write_xml(node_path, nodes)
    write_xml(edge_path, edges)
    with TemporaryDirectory(prefix="crowdsim-nanjing-build-") as directory:
        candidate = Path(directory) / "candidate.net.xml"
        command = [netconvert, "--node-files", str(node_path), "--edge-files", str(edge_path),
                   "--output-file", str(candidate), "--walkingareas", "true",
                   "--walkingareas.join-dist", "0", "--offset.disable-normalization", "true",
                   "--no-turnarounds", "true", "--junctions.corner-detail", "5",
                   "--geometry.remove", "false", "--junctions.join", "false", "--precision", "8"]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        root = ET.parse(candidate).getroot()
    # Plain XML used already projected local metres. Attach the inherited
    # projection without projecting these coordinates a second time.
    location = root.find("location")
    source_location = source_root.find("location")
    location.set("netOffset", source_location.get("netOffset"))
    location.set("projParameter", source_location.get("projParameter"))
    boundary = list(map(float, location.get("convBoundary").split(",")))
    corners = [source_net.convertXY2LonLat(x, y) for x in boundary[::2] for y in boundary[1::2]]
    location.set("origBoundary", ",".join(f"{value:.10f}" for value in
                  (min(p[0] for p in corners), min(p[1] for p in corners),
                   max(p[0] for p in corners), max(p[1] for p in corners))))
    original_areas = source_root.findall("edge[@function='walkingarea']")
    node_by_id = {node["id"]: node for node in spec["nodes"]}
    walking_metadata = []
    for area in root.findall("edge[@function='walkingarea']"):
        node_id = area.get("id")[1:].rsplit("_w", 1)[0]
        degree = len(incident[node_id])
        minimum = policy["four_arm_walking_area_m"] if degree >= 4 else policy["minimum_walking_area_m"]
        source_id = node_by_id[node_id]["source_junction_id"]
        inherited = [float(lane.get("width", "0")) for edge in original_areas
                     if source_id and edge.get("id").startswith(f":{source_id}_w")
                     for lane in edge.findall("lane")]
        width = max(minimum, *inherited, policy["walking_area_overrides"].get(node_id, 0))
        for lane in area.findall("lane"):
            width = max(width, float(lane.get("width", "0")))
            lane.set("width", f"{width:.8f}")
        walking_metadata.append({"junction": node_id, "walking_area": area.get("id"),
                                 "width_m": width, "minimum_m": minimum,
                                 "incident_roads": incident[node_id]})
    expected_areas = {node for node, roads in incident.items() if len(roads) >= 2}
    if {item["junction"] for item in walking_metadata} != expected_areas:
        raise ValueError("Missing real SUMO pedestrian junction connections")
    root.insert(0, ET.Comment("Pedestrian scenario generated by scripts/build_east_nanjing_network.py; width policy is applied on every build."))
    network_path = output / "east_nanjing.net.xml"
    write_xml(network_path, root)
    write_xml(output / "demo.rou.xml", demand_xml(route_cases(network_path)))
    normal = {e.get("id"): e for e in root.findall("edge") if e.get("function", "normal") == "normal"}
    if set(normal) != {road["id"] for road in spec["roads"]}:
        raise ValueError("netconvert changed the selected road set")
    for road in road_metadata:
        road["sumo_lane_length_m"] = float(normal[road["id"]].find("lane").get("length"))
    if source_hash != digest(source_path):
        raise ValueError("Source network changed during the build")
    manifest = {"schema_version": 1, "scenario_id": spec["scenario_id"],
                "source_network": spec["base_network"], "source_sha256": source_hash,
                "spec_sha256": digest(spec_path), "network_sha256": digest(network_path),
                "pedestrian_only": True, "roads": road_metadata, "walking_areas": walking_metadata,
                "terminal_nodes": sorted(set(incident) - expected_areas),
                "counts": {"roads": len(normal), "junctions": len(incident),
                           "walking_areas": len(walking_metadata)},
                "netconvert_warnings": result.stderr.strip(),
                "generation_rule": "SUMO junction width attrs are enforced after netconvert, never by reducing jamtime"}
    (output / "build_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SCENE / "network_spec.json")
    parser.add_argument("--output", type=Path, default=SCENE)
    parser.add_argument("--netconvert", default="netconvert")
    args = parser.parse_args()
    manifest = build(args.spec, args.output, args.netconvert)
    print(json.dumps({"output": str(args.output / "east_nanjing.net.xml"),
                      "counts": manifest["counts"], "source_unchanged": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
