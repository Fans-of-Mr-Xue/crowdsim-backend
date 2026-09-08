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
    def __init__(self, config_path: str | Path = DEFAULT_CONFIG, *, seed: int | None = None) -> None:
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.seed = int(self.config["seed"] if seed is None else seed)

    def sample_profile(self, person_id: str) -> AgentProfile:
        digest = hashlib.sha256(f"{self.seed}:{person_id}".encode("utf-8")).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        active = self.config["active_attributes"]

        def uniform(name: str) -> float:
            spec = active[name]
            return rng.uniform(float(spec["min"]), float(spec["max"]))

        trust = {name: rng.uniform(float(bounds[0]), float(bounds[1])) for name, bounds in active["information_trust"].items()}
        background = {name: rng.choices(spec["choices"], weights=spec["weights"], k=1)[0] for name, spec in self.config["background_attributes"].items()}
        return AgentProfile(person_id=person_id, free_walking_speed=uniform("free_walking_speed"), mobility=uniform("mobility"), perception_radius=uniform("perception_radius"), familiarity=uniform("familiarity"), risk_tolerance=uniform("risk_tolerance"), patience=uniform("patience"), crowding_tolerance=uniform("crowding_tolerance"), following_tendency=uniform("following_tendency"), authority_compliance=uniform("authority_compliance"), information_trust=trust, group_cohesion=uniform("group_cohesion"), stress_susceptibility=uniform("stress_susceptibility"), recovery_seconds=uniform("recovery_seconds"), endurance=uniform("endurance"), **background)


def sample_profile(person_id: str, seed: int = 20260908) -> AgentProfile:
    return PopulationProfileSampler(seed=seed).sample_profile(person_id)
