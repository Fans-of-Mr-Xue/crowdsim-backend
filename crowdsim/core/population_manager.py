"""SUMO person-demand bookkeeping without synthetic replenishment."""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
from pathlib import Path
from typing import Dict, Iterable, Optional
import xml.etree.ElementTree as ET

from crowdsim.domain.crowdsim_models import AgentState
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
        self.states: Dict[str, AgentState] = {}
        self.profiles = {}
        self.profile_sampler = profile_sampler or PopulationProfileSampler()
        self._previous_active: set[str] = set()
        self.locked_itinerary_ids = self._read_locked_itineraries()

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

    def _read_locked_itineraries(self) -> set[str]:
        locked: set[str] = set()
        for route_file in self.route_files:
            if not route_file.is_file():
                continue
            root = ET.parse(route_file).getroot()
            for person in root.findall("person"):
                if any(param.get("key") == "crowdsim.itinerary_locked" and param.get("value", "").lower() == "true" for param in person.findall("param")):
                    locked.add(person.get("id"))
        return locked

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
        return output

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
