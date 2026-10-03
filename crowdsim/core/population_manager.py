"""SUMO person-demand bookkeeping without synthetic replenishment."""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
import math
from pathlib import Path
from typing import Dict, Iterable, Optional
import xml.etree.ElementTree as ET

from crowdsim.domain.crowdsim_models import AgentState
from crowdsim.domain.person_parameters import (
    GOAL_LOCK_PARAM,
    HOTSPOT_DWELL_SECONDS_PARAM,
    HOTSPOT_ENTRY_EDGE_PARAM,
    HOTSPOT_ID_PARAM,
    HOTSPOT_PARK_ENTRY_EDGE_PARAM,
    HOTSPOT_RELEASE_TIME_PARAM,
    HOTSPOT_TARGET_EDGE_PARAM,
    ITINERARY_LOCK_PARAM,
)
from crowdsim.domain.population_profiles import PopulationProfileSampler
from crowdsim.infrastructure.sumo_adapter import SumoStepResult


@dataclass
class PopulationLedger:
    planned_ids: set[str] = field(default_factory=set)
    departed_ids: set[str] = field(default_factory=set)
    active_ids: set[str] = field(default_factory=set)
    arrived_ids: set[str] = field(default_factory=set)
    explicitly_removed: Dict[str, str] = field(default_factory=dict)
    unknown_disappearances: set[str] = field(default_factory=set)

    @property
    def conservation_error(self) -> int:
        accounted = len(self.active_ids) + len(self.arrived_ids) + len(self.explicitly_removed)
        return len(self.departed_ids) - accounted


class PopulationManager:
    def __init__(self, route_files: Iterable[str | Path] = (), profile_sampler: PopulationProfileSampler | None = None) -> None:
        self.route_files = tuple(Path(path).resolve() for path in route_files)
        self.ledger = PopulationLedger(planned_ids=self._read_planned_ids())
        # Preserve source capability even after a configured count of zero.
        self.count_configurable = bool(self.ledger.planned_ids)
        self.states: Dict[str, AgentState] = {}
        self.profiles = {}
        self.profile_sampler = profile_sampler or PopulationProfileSampler()
        self._previous_active: set[str] = set()
        (
            self.locked_itinerary_ids,
            self.goal_locked_hotspot_ids,
            self.hotspot_ids,
            self.hotspot_target_edges,
            self.hotspot_target_positions,
            self.hotspot_dwell_seconds,
            self.hotspot_release_times,
            self.hotspot_initial_entry_edges,
            self.hotspot_park_entry_edges,
        ) = self._read_route_metadata()

    def _read_planned_ids(self) -> set[str]:
        planned: set[str] = set()
        for route_file in self.route_files:
            if not route_file.is_file():
                continue
            for _, element in ET.iterparse(route_file, events=("end",)):
                if element.tag == "person" and element.get("id"):
                    planned.add(element.get("id"))
                element.clear()
        return planned

    def _read_route_metadata(self):
        locked: set[str] = set()
        goal_locked_hotspots: Dict[str, str] = {}
        hotspot_ids: Dict[str, str] = {}
        hotspot_target_edges: Dict[str, str] = {}
        hotspot_target_positions: Dict[str, float] = {}
        hotspot_dwell_seconds: Dict[str, float] = {}
        hotspot_release_times: Dict[str, float] = {}
        hotspot_initial_entry_edges: Dict[str, str] = {}
        hotspot_park_entry_edges: Dict[str, str] = {}
        for route_file in self.route_files:
            if not route_file.is_file():
                continue
            root = ET.parse(route_file).getroot()
            metadata = self._metadata_from_people(root.findall("person"))
            locked.update(metadata[0])
            goal_locked_hotspots.update(metadata[1])
            hotspot_ids.update(metadata[2])
            hotspot_target_edges.update(metadata[3])
            hotspot_target_positions.update(metadata[4])
            hotspot_dwell_seconds.update(metadata[5])
            hotspot_release_times.update(metadata[6])
            hotspot_initial_entry_edges.update(metadata[7])
            hotspot_park_entry_edges.update(metadata[8])
        return (
            locked,
            goal_locked_hotspots,
            hotspot_ids,
            hotspot_target_edges,
            hotspot_target_positions,
            hotspot_dwell_seconds,
            hotspot_release_times,
            hotspot_initial_entry_edges,
            hotspot_park_entry_edges,
        )

    def hotspot_id_for(self, person_id: str) -> str | None:
        return self.hotspot_ids.get(person_id)

    def hotspot_target_edge_for(self, person_id: str) -> str | None:
        return self.hotspot_target_edges.get(person_id)

    def hotspot_target_position_for(self, person_id: str) -> float | None:
        return self.hotspot_target_positions.get(person_id)

    def reconcile(self, step: SumoStepResult) -> None:
        current = set(step.persons)
        departed = set(step.departed_person_ids)
        arrived = set(step.arrived_person_ids)
        for person_id in current | departed:
            self.states.setdefault(person_id, AgentState(person_id=person_id))
            self.profiles.setdefault(person_id, self.profile_sampler.sample_profile(person_id))
        self.ledger.departed_ids.update(departed)
        # A person may be visible on the first queried boundary even if the caller
        # did not observe its exact departure list (for example after reconnect).
        self.ledger.departed_ids.update(current)
        self.ledger.arrived_ids.update(arrived)
        disappeared = self._previous_active - current - arrived - set(self.ledger.explicitly_removed)
        self.ledger.unknown_disappearances.update(disappeared)
        self.ledger.active_ids = current
        self._previous_active = current

    def mark_removed(self, person_id: str, reason: str) -> None:
        if person_id in self.ledger.arrived_ids:
            raise ValueError(f"{person_id} already classified as normally arrived")
        self.ledger.explicitly_removed[person_id] = reason
        self.ledger.active_ids.discard(person_id)

    def state_for(self, person_id: str) -> Optional[AgentState]:
        return self.states.get(person_id)

    def profile_for(self, person_id: str):
        return self.profiles.get(person_id) or self.profile_sampler.sample_profile(person_id)

    def commit_states(self, states: Dict[str, AgentState]) -> None:
        self.states.update(states)

    def prepare_demand(self, output_path: str | Path, count: int | None = None) -> Path:
        if len(self.route_files) != 1:
            raise ValueError("prepared pedestrian demand requires exactly one source route file")
        source = self.route_files[0]
        tree = ET.parse(source)
        root = tree.getroot()
        people = list(root.findall("person"))
        if not people:
            raise ValueError("source demand has no explicit person elements")
        target = len(people) if count is None else int(count)
        if target < 0:
            raise ValueError("count must be non-negative")
        selected = []
        if target <= len(people):
            if target:
                indexes = [min(len(people) - 1, int(index * len(people) / target)) for index in range(target)]
                selected = [copy.deepcopy(people[index]) for index in indexes]
        else:
            for index in range(target):
                person = copy.deepcopy(people[index % len(people)])
                if index >= len(people):
                    person.set("id", f"{person.get('id')}__rep{index // len(people)}")
                selected.append(person)
        self.profile_sampler.prepare_population(person.get("id") for person in selected)
        for person in people:
            root.remove(person)
        existing_types = {element.get("id") for element in root.findall("vType")}
        for person in selected:
            person_id = person.get("id")
            profile = self.profile_sampler.sample_profile(person_id)
            self.profiles[person_id] = profile
            type_id = f"profile_{hashlib.sha1(person_id.encode('utf-8')).hexdigest()[:12]}"
            if type_id not in existing_types:
                root.insert(0, ET.Element("vType", {"id": type_id, "vClass": "pedestrian", "maxSpeed": f"{profile.free_walking_speed * profile.mobility:.6f}"}))
                existing_types.add(type_id)
            person.set("type", type_id)
            root.append(person)
        output = Path(output_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        ET.indent(tree, space="    ")
        tree.write(output, encoding="utf-8", xml_declaration=True)
        self.ledger.planned_ids = {person.get("id") for person in selected}
        (
            self.locked_itinerary_ids,
            self.goal_locked_hotspot_ids,
            self.hotspot_ids,
            self.hotspot_target_edges,
            self.hotspot_target_positions,
            self.hotspot_dwell_seconds,
            self.hotspot_release_times,
            self.hotspot_initial_entry_edges,
            self.hotspot_park_entry_edges,
        ) = self._metadata_from_people(selected)
        return output

    @staticmethod
    def _metadata_from_people(people):
        locked: set[str] = set()
        goal_locked_hotspots: Dict[str, str] = {}
        hotspot_ids: Dict[str, str] = {}
        hotspot_target_edges: Dict[str, str] = {}
        hotspot_target_positions: Dict[str, float] = {}
        hotspot_dwell_seconds: Dict[str, float] = {}
        hotspot_release_times: Dict[str, float] = {}
        hotspot_initial_entry_edges: Dict[str, str] = {}
        hotspot_park_entry_edges: Dict[str, str] = {}
        for person in people:
            person_id = person.get("id")
            params = {param.get("key"): param.get("value", "") for param in person.findall("param")}
            if params.get(ITINERARY_LOCK_PARAM, "").lower() == "true":
                locked.add(person_id)
            hotspot_id = params.get(HOTSPOT_ID_PARAM, "").strip()
            if hotspot_id:
                hotspot_ids[person_id] = hotspot_id
                if params.get(GOAL_LOCK_PARAM, "").lower() == "true":
                    goal_locked_hotspots[person_id] = hotspot_id
            target_edge = params.get(HOTSPOT_TARGET_EDGE_PARAM, "").strip()
            if target_edge:
                hotspot_target_edges[person_id] = target_edge
                target_position = PopulationManager._hotspot_target_position(person, target_edge)
                if target_position is not None:
                    hotspot_target_positions[person_id] = target_position
            dwell_seconds = PopulationManager._hotspot_dwell_seconds(person, params)
            if dwell_seconds is not None:
                hotspot_dwell_seconds[person_id] = dwell_seconds
            release_time = PopulationManager._hotspot_release_time(person, params)
            if release_time is not None:
                hotspot_release_times[person_id] = release_time
            initial_entry = params.get(HOTSPOT_ENTRY_EDGE_PARAM, "").strip()
            if initial_entry:
                hotspot_initial_entry_edges[person_id] = initial_entry
            park_entry = params.get(HOTSPOT_PARK_ENTRY_EDGE_PARAM, "").strip()
            if park_entry:
                hotspot_park_entry_edges[person_id] = park_entry
        return (
            locked,
            goal_locked_hotspots,
            hotspot_ids,
            hotspot_target_edges,
            hotspot_target_positions,
            hotspot_dwell_seconds,
            hotspot_release_times,
            hotspot_initial_entry_edges,
            hotspot_park_entry_edges,
        )

    @staticmethod
    def _hotspot_target_position(person, target_edge: str) -> float | None:
        """Read and cross-check the person-specific hotspot activity position."""
        walk_positions = []
        for walk in person.findall("walk"):
            edges = walk.get("edges", "").split()
            if edges and edges[-1] == target_edge and walk.get("arrivalPos") is not None:
                walk_positions.append(("walk arrivalPos", walk.get("arrivalPos")))
        stop_positions = []
        for stop in person.findall("stop"):
            lane = stop.get("lane", "")
            if (stop.get("actType") == "hotspot_visit"
                    and lane.rsplit("_", 1)[0] == target_edge
                    and stop.get("endPos") is not None):
                stop_positions.append(("stop endPos", stop.get("endPos")))
        raw_positions = walk_positions + stop_positions
        if not raw_positions:
            return None
        positions = []
        for source, raw in raw_positions:
            try:
                position = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"hotspot visitor {person.get('id')} has non-numeric {source}: {raw!r}"
                ) from exc
            if not math.isfinite(position) or position < 0:
                raise ValueError(
                    f"hotspot visitor {person.get('id')} has invalid {source}: {raw!r}"
                )
            positions.append(position)
        if any(not math.isclose(position, positions[0], abs_tol=1e-6) for position in positions[1:]):
            raise ValueError(
                f"hotspot visitor {person.get('id')} has inconsistent walk/stop positions "
                f"on {target_edge}: {positions}"
            )
        return positions[0]

    @staticmethod
    def _hotspot_dwell_seconds(person, params: Dict[str, str]) -> float | None:
        """Read runtime-controlled dwell duration, with legacy stop compatibility."""
        raw = params.get(HOTSPOT_DWELL_SECONDS_PARAM)
        source = HOTSPOT_DWELL_SECONDS_PARAM
        if raw is None:
            stop = next(
                (item for item in person.findall("stop") if item.get("actType") == "hotspot_visit"),
                None,
            )
            if stop is None:
                return None
            raw = stop.get("duration")
            source = "legacy hotspot stop duration"
        try:
            duration = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"hotspot visitor {person.get('id')} has non-numeric {source}: {raw!r}"
            ) from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(
                f"hotspot visitor {person.get('id')} has invalid {source}: {raw!r}"
            )
        return duration

    @staticmethod
    def _hotspot_release_time(person, params: Dict[str, str]) -> float | None:
        raw = params.get(HOTSPOT_RELEASE_TIME_PARAM)
        if raw is None:
            return None
        try:
            release_time = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"hotspot visitor {person.get('id')} has non-numeric "
                f"{HOTSPOT_RELEASE_TIME_PARAM}: {raw!r}"
            ) from exc
        if not math.isfinite(release_time) or release_time < 0.0:
            raise ValueError(
                f"hotspot visitor {person.get('id')} has invalid "
                f"{HOTSPOT_RELEASE_TIME_PARAM}: {raw!r}"
            )
        return release_time

    def diagnostics(self) -> dict:
        return {
            "planned": len(self.ledger.planned_ids),
            "departed": len(self.ledger.departed_ids),
            "active": len(self.ledger.active_ids),
            "arrived": len(self.ledger.arrived_ids),
            "explicitly_removed": len(self.ledger.explicitly_removed),
            "unknown_disappearances": sorted(self.ledger.unknown_disappearances),
            "conservation_error": self.ledger.conservation_error,
        }
