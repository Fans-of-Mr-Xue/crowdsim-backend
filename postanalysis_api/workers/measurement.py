"""Stream SUMO FCD into clipped 5 m grid measurements and bounded DB chunks."""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import math
from statistics import mean
import xml.etree.ElementTree as ET

from .memorial_scenario import ENTRY_EDGES, network

SAMPLE_SECONDS = 5


@lru_cache(maxsize=1)
def geometry():
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union
    net = network()
    regions, junctions = {}, {}
    ring_ids = [edge.getID() for edge in net.getEdges() if edge.getID().startswith("679361567")]
    for name, ids in {**{key: [edge] for key, edge in ENTRY_EDGES.items()}, "ring": ring_ids}.items():
        surfaces = []
        for edge_id in ids:
            edge = net.getEdge(edge_id)
            for lane in edge.getLanes():
                if lane.allows("pedestrian"):
                    surfaces.append(LineString(lane.getShape()).buffer(lane.getWidth() / 2, cap_style=2))
            for node in (edge.getFromNode(), edge.getToNode()):
                shape = node.getShape()
                if len(shape) >= 3:
                    polygon = Polygon(shape)
                    if polygon.is_valid:
                        junctions[node.getID()] = polygon
        regions[name] = unary_union(surfaces)
    regions["global"] = unary_union([*regions.values(), *junctions.values()])
    return regions


def cell_area(x, y):
    from shapely.geometry import box
    return geometry()["global"].intersection(box(x * 5, y * 5, x * 5 + 5, y * 5 + 5)).area


@lru_cache(maxsize=1)
def region_cells():
    from shapely.geometry import box
    result = {}
    for name in ENTRY_EDGES:
        surface = geometry()[name]
        left, bottom, right, top = surface.bounds
        result[name] = {(x, y) for x in range(math.floor(left / 5), math.floor(right / 5) + 1)
                        for y in range(math.floor(bottom / 5), math.floor(top / 5) + 1)
                        if surface.intersection(box(x * 5, y * 5, x * 5 + 5, y * 5 + 5)).area >= 0.01}
    return result


def measure(fcd_path, duration, archive, repository, targets, *, cancelled=lambda: False):
    cell_areas = {}
    peak_density = peak_pressure = risk_exposure = congestion_duration = speed_sum = 0.0
    person_samples = 0
    risk_ids, observed_ids = set(), set()
    congested = defaultdict(float)
    timeline, frames, sequence, points_in_chunk = [], [], 0, 0
    region_samples = defaultdict(list)
    # Per-region targets are compared to measurements, never forced into motion.
    all_targets = {(row["timeSeconds"], row["regionId"]): row for row in targets}
    calibration = []
    for event, element in ET.iterparse(fcd_path, events=("end",)):
        if element.tag != "timestep":
            continue
        if cancelled():
            raise InterruptedError("Post run cancelled")
        time = float(element.attrib["time"])
        if time >= duration or not math.isclose(time / SAMPLE_SECONDS, round(time / SAMPLE_SECONDS), abs_tol=1e-6):
            element.clear()
            continue
        people, cells, regions = [], defaultdict(list), defaultdict(list)
        for node in element.findall("person"):
            item = node.attrib
            speed, angle = float(item.get("speed", 0)), math.radians(float(item.get("angle", 0)))
            x, y = float(item["x"]), float(item["y"])
            edge = item.get("edge", item.get("lane", "").rsplit("_", 1)[0])
            person = {"id": item["id"], "x": x, "y": y, "speed": speed, "edgeId": edge,
                      "vx": speed * math.sin(angle), "vy": speed * math.cos(angle)}
            people.append(person)
            cells[(math.floor(x / 5), math.floor(y / 5))].append(person)
            for region, region_edge in ENTRY_EDGES.items():
                if edge == region_edge:
                    regions[region].append(person)
        frame_peak = frame_pressure = frame_risk = 0.0
        current_congested, cell_metrics = set(), {}
        for cell, occupants in cells.items():
            if cell not in cell_areas:
                cell_areas[cell] = cell_area(*cell)
            area = cell_areas[cell]
            # Missing or degenerate pedestrian geometry is a measurement error.
            if area < 0.01:
                raise ValueError(f"FCD position outside registered pedestrian geometry: {cell}")
            density = len(occupants) / area
            vx, vy = mean(p["vx"] for p in occupants), mean(p["vy"] for p in occupants)
            pressure = density * mean((p["vx"] - vx) ** 2 + (p["vy"] - vy) ** 2 for p in occupants)
            cell_metrics[cell] = (density, pressure)
            frame_peak, frame_pressure = max(frame_peak, density), max(frame_pressure, pressure)
            if density >= 3.5:
                frame_risk += len(occupants)
                risk_ids.update(p["id"] for p in occupants)
                if mean(p["speed"] for p in occupants) <= 0.5:
                    current_congested.add(cell)
        dt = min(SAMPLE_SECONDS, max(0, duration - time))
        for cell in list(congested):
            if cell not in current_congested:
                if congested[cell] >= 10:
                    congestion_duration += congested[cell]
                del congested[cell]
        for cell in current_congested:
            congested[cell] += dt
        risk_exposure += frame_risk * dt
        peak_density, peak_pressure = max(peak_density, frame_peak), max(peak_pressure, frame_pressure)
        speed_sum += sum(p["speed"] for p in people) * dt
        person_samples += len(people) * dt
        observed_ids.update(p["id"] for p in people)
        row = {"timeSeconds": time, "population": len(people), "density": frame_peak,
               "meanSpeed": mean(p["speed"] for p in people) if people else None,
               "pressureProxy": frame_pressure, "riskPopulation": frame_risk}
        # Timeline is bounded (~4k frames for the full 5.5h dataset).
        timeline.append(row)
        region_samples["global"].append(row)
        for region in ENTRY_EDGES:
            local = regions[region]
            local_row = {"timeSeconds": time, "population": len(local),
                         "density": max((cell_metrics.get(cell, (0, 0))[0] for cell in region_cells()[region]), default=0),
                         "meanSpeed": mean(p["speed"] for p in local) if local else None,
                         "pressureProxy": max((cell_metrics.get(cell, (0, 0))[1] for cell in region_cells()[region]), default=0)}
            region_samples[region].append(local_row)
            target = all_targets.get((int(time), region))
            if target:
                calibration.append({"timeSeconds": time, "regionId": region,
                                    "sourcePopulation": target["population"], "simulatedPopulation": len(local),
                                    "residual": len(local) - target["population"]})
        frames.append({"timeSeconds": time, "persons": people})
        points_in_chunk += len(people)
        if len(frames) >= 5 or points_in_chunk >= 10000:
            repository.append_trajectory(archive, sequence, frames)
            sequence, frames, points_in_chunk = sequence + 1, [], 0
        element.clear()
    if frames:
        repository.append_trajectory(archive, sequence, frames)
    congestion_duration += sum(value for value in congested.values() if value >= 10)
    metrics = {"peak_density": peak_density, "mean_speed": speed_sum / person_samples if person_samples else None,
               "peak_pressure_proxy": peak_pressure, "risk_exposed_unique": len(risk_ids),
               "risk_exposure": risk_exposure, "congestion_duration": congestion_duration,
               "evacuation_time": None}
    return {"metrics": metrics, "timeline": timeline, "regionSamples": dict(region_samples),
            "calibration": {"populationResiduals": calibration,
                            "calibrated": False, "status": "engineering_model_not_fitted",
                            "populationMAE": mean(abs(row["residual"]) for row in calibration) if calibration else None},
            "measurement": {"gridSizeMeters": 5, "samplingSeconds": SAMPLE_SECONDS,
                            "areaDefinition": "SUMO pedestrian lane buffers and registered junction polygons, clipped to each 5m grid",
                            "pressureDefinition": "density * vector velocity variance; not contact force",
                            "congestionUnit": "cell*s", "evacuationCensored": True,
                            "observedPersonCount": len(observed_ids)}}
