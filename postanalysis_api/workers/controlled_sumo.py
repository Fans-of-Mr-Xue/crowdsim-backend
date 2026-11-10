"""SUMO owns positions; TraCI only holds/release walkers before their destination.

SUMO's generic person waiting stage uses a roadside display offset. It must
not become the crowd's physical holding model. Hold a walking stage instead,
and remove the future waiting stage before releasing the person.
"""
from __future__ import annotations

import heapq
import json
import logging
from pathlib import Path
import time
import xml.etree.ElementTree as ET

from crowdsim.infrastructure.sumo_adapter import SumoAdapter, tc
from .memorial_scenario import NET_FILE
from .live_frames import capture_frame


def execute(binary, routes, folder: Path, extra_args, manifest, duration, cancelled, *, on_progress=None):
    config = ET.Element("configuration")
    inputs = ET.SubElement(config, "input")
    ET.SubElement(inputs, "net-file", value=str(NET_FILE))
    ET.SubElement(inputs, "route-files", value=str(routes))
    config_path = folder / "post.sumocfg"
    config_path.write_bytes(ET.tostring(config, encoding="utf-8", xml_declaration=True))
    (folder / "command.json").write_text(json.dumps([binary, "-c", str(config_path), "--start", "--quit-on-end", *extra_args], indent=2), encoding="utf-8")
    adapter = SumoAdapter(config_path, sumo_binary=binary, extra_args=extra_args)
    people = {person["id"]: person for person in manifest["cohorts"]}
    tracking, held, releases, events = set(), {}, [], []
    last_publish = float("-inf")
    frame_error_logged = False
    step_seconds = manifest.get('factualContext', {}).get('stepSeconds', 0.5)
    step_count = round(duration / step_seconds)
    if abs(step_count * step_seconds - duration) > 1e-8:
        raise ValueError('Execution window must align with the simulation step')
    try:
        adapter.start()
        connection = adapter.connection
        connection.simulation.subscribe((tc.VAR_DEPARTED_PERSONS_IDS, tc.VAR_TIME))
        for step in range(1, step_count + 1):
            if cancelled():
                raise InterruptedError()
            connection.simulationStep()
            simulation = connection.simulation.getSubscriptionResults()
            now = float(simulation[tc.VAR_TIME])
            for person_id in simulation[tc.VAR_DEPARTED_PERSONS_IDS]:
                person = people[person_id]
                if person["release"] <= now:
                    connection.person.removeStage(person_id, 1)
                    events.append({"timeSeconds": now, "personId": person_id, "type": "release_before_hold"})
                else:
                    tracking.add(person_id)
                    heapq.heappush(releases, (person["release"], person_id))
                    connection.person.subscribe(person_id, (tc.VAR_ROAD_ID, tc.VAR_LANEPOSITION))
            while releases and releases[0][0] <= now:
                _, person_id = heapq.heappop(releases)
                # Only remove future stage index 1. Never move/replace the current stage.
                connection.person.removeStage(person_id, 1)
                if person_id in held:
                    adapter.set_person_speed(person_id, held.pop(person_id))
                else:
                    connection.person.unsubscribe(person_id)
                tracking.discard(person_id)
                events.append({"timeSeconds": now, "personId": person_id, "type": "release"})
            for person_id, state in list(connection.person.getAllSubscriptionResults().items()):
                if person_id not in tracking or person_id in held:
                    continue
                person = people[person_id]
                if state.get(tc.VAR_ROAD_ID) == person["holdingEdge"] and abs(state.get(tc.VAR_LANEPOSITION, -1) - person["holdingPosition"]) <= 1.1:
                    if adapter.remaining_stage_count(person_id) != 3:
                        raise RuntimeError("Physical holding boundary missed; refusing roadside waiting coordinates")
                    held[person_id] = connection.person.getMaxSpeed(person_id)
                    adapter.set_person_speed(person_id, 0)
                    connection.person.unsubscribe(person_id)
                    events.append({"timeSeconds": now, "personId": person_id, "type": "hold",
                                   "edgeId": person["holdingEdge"], "targetPosition": person["holdingPosition"]})
            if step == step_count and abs(adapter.time_seconds - duration) > 0.01:
                raise RuntimeError("SUMO clock did not match the declared integration step")
            # The renderer polls a single latest frame; full FCD archival remains
            # in measurement.py. Do not write thousands of live frames to Mongo.
            wall_time = time.monotonic()
            if on_progress is not None and (wall_time - last_publish >= 0.5 or step == step_count):
                update = {"stepSeconds": now}
                try:
                    update["frame"] = capture_frame(connection, now)
                except Exception:
                    # Display availability must not change the scientific run.
                    update.update(frame=None, frameError="SUMO_LIVE_FRAME_UNAVAILABLE")
                    if not frame_error_logged:
                        logging.getLogger(__name__).exception("Could not read the post SUMO display frame")
                        frame_error_logged = True
                on_progress(update)
                last_publish = time.monotonic()
        return events
    finally:
        adapter.close()
