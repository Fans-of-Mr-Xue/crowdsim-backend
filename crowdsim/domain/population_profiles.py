"""Deterministic profile sampling without culture-to-behavior lookup tables."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
from typing import Any

from crowdsim.domain.crowdsim_models import AgentProfile


DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "population_profiles.json"


class PopulationProfileSampler:
    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG,
        *,
        seed: int | None = None,
        distributions: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.seed = int(self.config["seed"] if seed is None else seed)
        self.distributions = distributions or {}
        self.agent_type_attributes: dict[str, dict[str, dict[str, float]]] = {}
        self._assignments: dict[str, dict[str, str]] = {}

    def configure_distributions(self, distributions: dict[str, list[dict[str, Any]]]) -> None:
        self.distributions = {
            str(name): [dict(item) for item in rows]
            for name, rows in distributions.items()
        }
        self._assignments = {}

    def configure_active_attributes(self, overrides: dict[str, dict[str, Any]]) -> None:
        """Apply validated per-run sampling ranges to known profile attributes."""
        active = self.config["active_attributes"]
        for name, values in overrides.items():
            if name not in active or name == "information_trust" or not isinstance(values, dict):
                raise ValueError(f"unsupported population profile parameter: {name}")
            minimum, maximum = float(values["min"]), float(values["max"])
            if minimum > maximum:
                raise ValueError(f"population profile range is reversed: {name}")
            active[name] = {**active[name], "min": minimum, "max": maximum}

    def configure_agent_types(self, agent_types: list[dict[str, Any]]) -> None:
        supported = set(self.config["active_attributes"]) - {"information_trust"}
        configured: dict[str, dict[str, dict[str, float]]] = {}
        for agent in agent_types:
            role_id = str(agent.get("id") or "")
            if not role_id:
                raise ValueError("agent type id is required")
            attributes: dict[str, dict[str, float]] = {}
            for name, values in dict(agent.get("attributes") or {}).items():
                if name not in supported or not isinstance(values, dict):
                    continue
                minimum, maximum = float(values["min"]), float(values["max"])
                if minimum > maximum:
                    raise ValueError(f"agent type profile range is reversed: {role_id}.{name}")
                attributes[name] = {"min": minimum, "max": maximum}
            configured[role_id] = attributes
        self.agent_type_attributes = configured

    def prepare_population(self, person_ids) -> None:
        """Allocate exact integer category counts with deterministic shuffling."""
        people = sorted(str(person_id) for person_id in person_ids)
        self._assignments = {person_id: {} for person_id in people}
        for dimension, rows in self.distributions.items():
            if not rows or not people:
                continue
            raw_counts = [len(people) * float(item["percent"]) / 100.0 for item in rows]
            counts = [int(value) for value in raw_counts]
            remainder = len(people) - sum(counts)
            order = sorted(
                range(len(rows)),
                key=lambda index: (-(raw_counts[index] - counts[index]), index),
            )
            for index in order[:remainder]:
                counts[index] += 1
            rng_seed = int.from_bytes(
                hashlib.sha256(f"{self.seed}:{dimension}".encode("utf-8")).digest()[:8],
                "big",
            )
            shuffled = people[:]
            random.Random(rng_seed).shuffle(shuffled)
            offset = 0
            for item, count in zip(rows, counts):
                value = str(
                    item["label"] if dimension == "origin"
                    else item.get("code") or item.get("id") or item["label"]
                )
                for person_id in shuffled[offset:offset + count]:
                    self._assignments[person_id][dimension] = value
                offset += count

    def sample_profile(self, person_id: str) -> AgentProfile:
        digest = hashlib.sha256(f"{self.seed}:{person_id}".encode("utf-8")).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        active = self.config["active_attributes"]
        assigned = dict(self._assignments.get(person_id, {}))
        role_attributes = self.agent_type_attributes.get(assigned.get("crowd_role", ""), {})

        def uniform(name: str) -> float:
            spec = role_attributes.get(name, active[name])
            return rng.uniform(float(spec["min"]), float(spec["max"]))

        trust = {name: rng.uniform(float(bounds[0]), float(bounds[1])) for name, bounds in active["information_trust"].items()}
        background = {name: rng.choices(spec["choices"], weights=spec["weights"], k=1)[0] for name, spec in self.config["background_attributes"].items()}
        for dimension, rows in self.distributions.items():
            if dimension in assigned or not rows:
                continue
            assigned[dimension] = str(rng.choices(
                [
                    item["label"] if dimension == "origin"
                    else item.get("code") or item.get("id") or item["label"]
                    for item in rows
                ],
                weights=[float(item["percent"]) for item in rows],
                k=1,
            )[0])
        age_band = assigned.get("age_band", "unspecified")
        age_group = {
            "age_0_17": "young",
            "age_18_34": "adult",
            "age_35_54": "adult",
            "age_55_64": "senior",
            "age_65_plus": "senior",
        }.get(age_band, background["age_group"])
        origin = assigned.get("origin", "unspecified")
        return AgentProfile(
            person_id=person_id,
            free_walking_speed=uniform("free_walking_speed"),
            mobility=uniform("mobility"),
            perception_radius=uniform("perception_radius"),
            familiarity=uniform("familiarity"),
            risk_tolerance=uniform("risk_tolerance"),
            patience=uniform("patience"),
            crowding_tolerance=uniform("crowding_tolerance"),
            following_tendency=uniform("following_tendency"),
            authority_compliance=uniform("authority_compliance"),
            information_trust=trust,
            group_cohesion=uniform("group_cohesion"),
            stress_susceptibility=uniform("stress_susceptibility"),
            recovery_seconds=uniform("recovery_seconds"),
            endurance=uniform("endurance"),
            **{
                **background,
                "age_group": age_group,
                "nationality": origin if origin != "unspecified" else background["nationality"],
            },
            crowd_role=assigned.get("crowd_role", "unspecified"),
            age_band=age_band,
            gender=assigned.get("gender", "unspecified"),
            origin=origin,
        )


def sample_profile(person_id: str, seed: int = 20260908) -> AgentProfile:
    return PopulationProfileSampler(seed=seed).sample_profile(person_id)
