"""Bounded display frames from real SUMO coordinates; archives remain lossless."""
from __future__ import annotations

from copy import deepcopy
import math

from .memorial_scenario import network

MAX_LIVE_PERSONS = 5000


def geographic_person(person):
    """Use the same SUMO network projection for both live and archived frames."""
    item = dict(person)
    lng, lat = network().convertXY2LonLat(float(item["x"]), float(item["y"]))
    if not (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90):
        raise ValueError("SUMO network did not produce valid WGS84 coordinates")
    item.update(lng=float(lng), lat=float(lat))
    return item


def capture_frame(connection, time_seconds):
    identifiers = sorted(connection.person.getIDList())
    persons = []
    for person_id in identifiers[:MAX_LIVE_PERSONS]:
        x, y = connection.person.getPosition(person_id)
        persons.append(geographic_person({
            "id": person_id, "x": float(x), "y": float(y),
            "speed": float(connection.person.getSpeed(person_id)),
            "angle": float(connection.person.getAngle(person_id)),
            "edgeId": connection.person.getRoadID(person_id),
        }))
    return {"timeSeconds": time_seconds, "persons": persons, "coordinateSystem": "WGS84",
            "source": "sumo-traci", "totalPersons": len(identifiers),
            "truncated": len(identifiers) > MAX_LIVE_PERSONS}


def geographic_trajectory(payload):
    """Enrich the HTTP response only, never rewrite stored trajectory chunks."""
    result = deepcopy(payload)
    result.update(coordinateSystem="WGS84", source="sumo-fcd")
    for chunk in result.get("items", []):
        for frame in chunk.get("frames", []):
            frame["persons"] = [geographic_person(person) for person in frame.get("persons", [])]
            frame.update(coordinateSystem="WGS84", source="sumo-fcd")
    return result
