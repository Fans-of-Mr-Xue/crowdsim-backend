"""Build background-flow plus finite hotspot-visit pedestrian demand."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
import xml.etree.ElementTree as ET

import sumolib


LOCK_PARAM = "crowdsim.itinerary_locked"


def build_hotspot_demand(
    source_path: str | Path,
    network_path: str | Path,
    hotspot_config_path: str | Path,
    output_path: str | Path,
    *,
    hotspot_id: str = "chen_yi_square",
    seed: int = 20260908,
    visitor_count: int | None = None,
    background_count: int | None = None,
) -> dict:
    """Generate explicit people; no person is created after SUMO starts."""
    source_path = Path(source_path)
    output_path = Path(output_path)
    payload = json.loads(Path(hotspot_config_path).read_text(encoding="utf-8"))
    hotspot = next((item for item in payload.get("hotspots", ()) if item.get("id") == hotspot_id), None)
    if hotspot is None:
        raise ValueError(f"unknown hotspot: {hotspot_id}")
    target_edge = str(hotspot["target_edge"])
    visitor_count = int(hotspot["visitor_count"] if visitor_count is None else visitor_count)
    background_count = int(hotspot["background_count"] if background_count is None else background_count)
    if visitor_count < 0 or background_count < 0:
        raise ValueError("demand counts must be non-negative")

    tree = ET.parse(source_path)
    source_root = tree.getroot()
    source_people = list(source_root.findall("person"))
    if not source_people:
        raise ValueError("source demand contains no explicit people")
    network = sumolib.net.readNet(str(network_path), withInternal=True)
    edge = network.getEdge(target_edge)
    pedestrian_lanes = [lane for lane in edge.getLanes() if lane.allows("pedestrian")]
    if not pedestrian_lanes:
        raise ValueError(f"target edge does not allow pedestrians: {target_edge}")

    templates = []
    for person in source_people:
        walk = person.find("walk")
        edges = walk.get("edges", "").split() if walk is not None else []
        if target_edge not in edges:
            continue
        target_index = edges.index(target_edge)
        if target_index == 0 or target_index >= len(edges) - 1:
            continue
        approach_limit = float(hotspot.get("approach_max_distance_meters", 700.0))
        start_index = target_index - 1
        prefix_distance = network.getEdge(target_edge).getLength() + network.getEdge(edges[start_index]).getLength()
        while start_index > 0:
            candidate_length = network.getEdge(edges[start_index - 1]).getLength()
            if prefix_distance + candidate_length > approach_limit:
                break
            start_index -= 1
            prefix_distance += candidate_length
        trimmed_edges = edges[start_index:]
        trimmed_target_index = target_index - start_index
        templates.append((trimmed_edges, trimmed_target_index, prefix_distance))
    incoming_directions = {(edges[index - 1], edges[index + 1]) for edges, index, _ in templates}
    if not templates or len(incoming_directions) < 2:
        raise ValueError("source demand needs short, valid hotspot routes from at least two directions")

    rng = random.Random(seed)
    background = _even_sample(source_people, background_count)
    visitors = []
    arrival_start, arrival_end = map(float, hotspot["arrival_window_seconds"])
    dwell_start, dwell_end = map(float, hotspot["dwell_seconds"])
    reference_speed = float(hotspot["reference_walking_speed_mps"])
    if not 0 < reference_speed or arrival_end < arrival_start or dwell_end < dwell_start:
        raise ValueError("invalid hotspot timing configuration")

    for index in range(visitor_count):
        edges, target_index, prefix_distance = templates[index % len(templates)]
        desired_arrival = rng.uniform(arrival_start, arrival_end)
        depart = max(0.0, desired_arrival - prefix_distance / reference_speed)
        stop_position = rng.uniform(2.0, max(2.0, edge.getLength() - 2.0))
        lane = pedestrian_lanes[index % len(pedestrian_lanes)]
        duration = rng.uniform(dwell_start, dwell_end)
        person = ET.Element("person", {
            "id": f"hotspot.{hotspot_id}.{index:04d}",
            "depart": f"{depart:.2f}",
        })
        ET.SubElement(person, "param", {"key": LOCK_PARAM, "value": "true"})
        ET.SubElement(person, "param", {"key": "crowdsim.cohort", "value": "hotspot_visitor"})
        ET.SubElement(person, "param", {"key": "crowdsim.hotspot_id", "value": hotspot_id})
        ET.SubElement(person, "walk", {
            "edges": " ".join(edges[: target_index + 1]),
            "arrivalPos": f"{stop_position:.2f}",
        })
        ET.SubElement(person, "stop", {
            "lane": lane.getID(),
            "endPos": f"{stop_position:.2f}",
            "duration": f"{duration:.2f}",
            "actType": "hotspot_visit",
        })
        ET.SubElement(person, "walk", {
            "edges": " ".join(edges[target_index:]),
        })
        visitors.append(person)

    for person in source_people:
        source_root.remove(person)
    combined = background + visitors
    combined.sort(key=lambda person: (float(person.get("depart", 0)), person.get("id", "")))
    for person in combined:
        source_root.append(person)
    source_root.insert(0, ET.Comment(
        f"background={len(background)} hotspot={hotspot_id} visitors={len(visitors)} seed={seed}; "
        "hotspot timing and volume are engineering assumptions pending Bund calibration"
    ))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree, space="    ")
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return {
        "hotspot_id": hotspot_id,
        "target_edge": target_edge,
        "background_count": len(background),
        "visitor_count": len(visitors),
        "total_count": len(combined),
        "template_count": len(templates),
        "incoming_direction_count": len(incoming_directions),
        "output": str(output_path.resolve()),
        "parameter_status": payload.get("parameter_status", "unspecified"),
    }


def _even_sample(items: list[ET.Element], count: int) -> list[ET.Element]:
    if count == 0:
        return []
    return [copy.deepcopy(items[min(len(items) - 1, int(index * len(items) / count))]) for index in range(min(count, len(items)))]
