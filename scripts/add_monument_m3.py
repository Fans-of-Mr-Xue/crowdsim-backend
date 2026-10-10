"""Build the M3 pedestrian patch and merge only its local SUMO elements.

netconvert builds junction geometry, not a simulation.  The scoped merge keeps
unrelated geometry, signals, permissions and manually widened walking areas
unchanged even when the installed netconvert is newer than the base network.
Hotspot configuration and demand migration are documented separately.
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "scenarios" / "shanghai_bund"
JUNCTIONS = {
    "6361541057", "6361541058", "8417005112",
    "monument_m3_ring_junction",
}
NEW_EDGES = {
    "679361567#2_m3_north", "monument_m3",
}
GEOMETRY_JUNCTIONS = {"8417005112", "monument_m3_ring_junction"}
BOUNDARY_JUNCTIONS = JUNCTIONS - GEOMETRY_JUNCTIONS


def local_internal(identifier: str) -> bool:
    return any(identifier.startswith(f":{node}_") for node in JUNCTIONS)


def connection_is_local(element: ET.Element) -> bool:
    return any(local_internal(element.get(key, "")) for key in ("from", "to", "via"))


def render(element: ET.Element) -> str:
    element = copy.deepcopy(element)
    element.tail = None
    ET.indent(element, space="    ", level=1)
    return "    " + ET.tostring(element, encoding="unicode", short_empty_elements=True).replace(" />", "/>")


def canonical(element: ET.Element) -> tuple:
    return element.tag, tuple(sorted(element.attrib.items())), tuple(canonical(child) for child in element)


def merge_local(base_text: str, candidate: ET.Element) -> str:
    base = ET.fromstring(base_text)
    normal_ids = {
        edge.get("id") for edge in base.findall("edge")
        if edge.get("from") in JUNCTIONS or edge.get("to") in JUNCTIONS
    } | NEW_EDGES
    originals = {edge.get("id"): edge for edge in base.findall("edge")}
    replacement_edges = {}
    for edge in candidate.findall("edge"):
        identifier = edge.get("id")
        if identifier not in normal_ids and not local_internal(identifier):
            continue
        previous = originals.get(identifier)
        if previous is not None:
            # Splitting H2 does not change the tangents at its original ends.
            # Keep their geometry and all unmodified incident lanes verbatim.
            keep_boundary = any(identifier.startswith(f":{node}_") for node in BOUNDARY_JUNCTIONS)
            keep_normal = (
                identifier != "679361567#2" and not local_internal(identifier)
                and edge.get("from") not in GEOMETRY_JUNCTIONS
                and edge.get("to") not in GEOMETRY_JUNCTIONS
            )
            if keep_boundary or keep_normal:
                edge = copy.deepcopy(previous)
        if edge.get("function") == "walkingarea":
            previous = originals.get(identifier)
            for lane in edge.findall("lane"):
                old_lane = previous.find("lane") if previous is not None else None
                lane.set("width", old_lane.get("width", "4.00") if old_lane is not None else "4.00")
        replacement_edges[identifier] = edge
    missing = normal_ids - replacement_edges.keys()
    if missing:
        raise ValueError(f"netconvert omitted local edges: {sorted(missing)}")

    def replace_edge(match):
        element = ET.fromstring(match.group().strip())
        identifier = element.get("id")
        if identifier in normal_ids or local_internal(identifier):
            replacement = replacement_edges.pop(identifier, None)
            if replacement is not None and canonical(replacement) == canonical(element):
                return match.group()
            return render(replacement) if replacement is not None else ""
        return match.group()

    result = re.sub(r"^    <edge\b[^>]*>.*?^    </edge>", replace_edge, base_text, flags=re.M | re.S)
    # New normal and walking-area edges must precede junctions in the net schema.
    new_edges = "\n\n".join(render(replacement_edges[key]) for key in sorted(replacement_edges))
    edge_end = re.search(r"^    <(?:tlLogic|junction)\b", result, flags=re.M)
    if edge_end is None:
        raise ValueError("base network has no junctions")
    result = result[:edge_end.start()] + new_edges + "\n\n" + result[edge_end.start():]

    replacements = {node.get("id"): node for node in candidate.findall("junction") if node.get("id") in JUNCTIONS}
    if set(replacements) != JUNCTIONS:
        raise ValueError("netconvert omitted local junctions")
    original_junctions = {node.get("id"): node for node in base.findall("junction")}
    for identifier in BOUNDARY_JUNCTIONS:
        replacements[identifier].set("shape", original_junctions[identifier].get("shape"))

    def replace_junction(match):
        element = ET.fromstring(match.group().strip())
        identifier = element.get("id")
        if identifier not in replacements:
            return match.group()
        replacement = replacements.pop(identifier)
        return match.group() if canonical(element) == canonical(replacement) else render(replacement)

    result = re.sub(r"^    <junction\b[^\n]*?/>|^    <junction\b[^\n]*>.*?^    </junction>", replace_junction, result, flags=re.M | re.S)
    new_junctions = "\n\n".join(render(replacements[key]) for key in sorted(replacements))
    connection_start = re.search(r"^    <connection\b", result, flags=re.M)
    if connection_start is None:
        raise ValueError("base network has no connections")
    result = result[:connection_start.start()] + new_junctions + "\n\n" + result[connection_start.start():]

    replacement_connections = {
        canonical(item): item for item in candidate.findall("connection") if connection_is_local(item)
    }

    def replace_connection(match):
        element = ET.fromstring(match.group().strip())
        if not connection_is_local(element):
            return match.group()
        replacement = replacement_connections.pop(canonical(element), None)
        return match.group() if replacement is not None else ""

    result = re.sub(r"^    <connection\b[^\n]*?/>\n?|^    <connection\b[^\n]*>.*?^    </connection>\n?", replace_connection, result, flags=re.M | re.S)
    connections = [render(item) for item in replacement_connections.values()]
    connection_start = re.search(r"^    <connection\b", result, flags=re.M)
    result = result[:connection_start.start()] + "\n".join(connections) + "\n\n" + result[connection_start.start():]
    # Keep the base format/version and original provenance; identify the local edit.
    result = result.replace("<net version=", "<!-- Local M3 patch: scripts/add_monument_m3.py; manually assumed geometry. -->\n<net version=", 1)
    merged = ET.fromstring(result)
    order = {tag: index for index, tag in enumerate(
        ("location", "type", "edge", "tlLogic", "junction", "connection", "prohibition", "roundabout", "taz")
    )}
    ranks = [order[element.tag] for element in merged]
    if ranks != sorted(ranks):
        raise ValueError("merged network violates the SUMO element order")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--output", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--netconvert", default="netconvert")
    args = parser.parse_args()
    base_text = args.input.read_text(encoding="utf-8")
    present = {edge.get("id") for edge in ET.fromstring(base_text).findall("edge")}
    if present & NEW_EDGES:
        if not NEW_EDGES.issubset(present):
            raise ValueError("base network contains an incomplete M3 patch")
        print("M3 is already present; network left unchanged.")
        return
    with TemporaryDirectory(prefix="crowdsim-m3-") as temporary:
        candidate_path = Path(temporary) / "candidate.net.xml"
        subprocess.run([
            args.netconvert, "--sumo-net-file", str(args.input),
            "--node-files", str(SCENARIO / "patches" / "monument_m3.nod.xml"),
            "--edge-files", str(SCENARIO / "patches" / "monument_m3.edg.xml"),
            "--output-file", str(candidate_path), "--walkingareas", "true",
            "--offset.disable-normalization", "true", "--no-turnarounds", "true",
            "--junctions.corner-detail", "5",
        ], check=True, capture_output=True, text=True)
        candidate = ET.parse(candidate_path).getroot()
        result = merge_local(base_text, candidate)
    args.output.write_text(result, encoding="utf-8")
    print(f"M3 local patch written to {args.output}")


if __name__ == "__main__":
    main()
