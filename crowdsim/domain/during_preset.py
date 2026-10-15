"""Versioned, server-owned during presets shared by experiments and runtime."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from crowdsim.domain.requirement_spec import RequirementSpec

ROOT = Path(__file__).resolve().parents[2]
PRESET_FILES = {"memorial-tower-during-v1": "config/during_presets/memorial_tower_v1.json"}


def preset_fingerprint(preset):
    encoded = json.dumps(preset, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_preset_file(path):
    preset = json.loads(Path(path).read_text(encoding="utf-8"))
    for name, expected in preset["resource_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"preset resource changed; revalidate and version the preset: {name}")
    RequirementSpec.parse(preset["requirement"])
    return preset


def load_during_preset(preset_id, *, version=None, fingerprint=None):
    if not isinstance(preset_id, str) or preset_id not in PRESET_FILES:
        raise ValueError(f"unknown during preset: {preset_id}")
    preset = load_preset_file(ROOT / PRESET_FILES[preset_id])
    if preset["status"] != "stage0_verified":
        raise ValueError("during preset has not completed stage-0 validation")
    if version is not None and (type(version) is not int or version != preset["version"]):
        raise ValueError("during preset version mismatch")
    if fingerprint is not None and fingerprint != preset_fingerprint(preset):
        raise ValueError("during preset changed since experiment creation")
    return preset


def bind_preset_scenario(scenario):
    scenario = deepcopy(scenario)
    allowed = {"presetId", "presetVersion", "presetFingerprint", "duringPreset", "population",
               "durationSeconds", "locationId", "seedSet", "speedFactor", "wallTimeoutSeconds",
               "statisticalValidation", "emergencyEvent"}
    unknown = set(scenario) - allowed
    if unknown:
        raise ValueError(f"unsupported during scenario fields: {', '.join(sorted(unknown))}")
    preset = load_during_preset(scenario["presetId"], version=scenario.get("presetVersion"),
                               fingerprint=scenario.get("presetFingerprint"))
    if "duringPreset" in scenario and scenario["duringPreset"] != preset:
        raise ValueError("experiment during preset snapshot mismatch")
    for key in ("speedFactor", "wallTimeoutSeconds"):
        value = scenario.get(key)
        if key in scenario and (isinstance(value, bool) or not isinstance(value, (int, float))
                                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"scenario.{key} must be finite and positive")
    fixed = {"population": preset["requirement"]["population"]["total"],
             "durationSeconds": preset["experiment"]["duration_seconds"], "locationId": preset["location_id"]}
    for key, value in fixed.items():
        if key in scenario and (isinstance(scenario[key], bool) or scenario[key] != value):
            raise ValueError(f"scenario.{key} must match the during preset ({value})")
        scenario[key] = value
    if scenario.get("emergencyEvent"):
        raise ValueError("during preset does not support an additional emergencyEvent")
    scenario.setdefault("seedSet", list(preset["experiment"]["seed_set"]))
    seeds = scenario["seedSet"]
    if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 30
            or any(type(seed) is not int or not 0 <= seed <= 2147483647 for seed in seeds)
            or len(set(seeds)) != len(seeds)):
        raise ValueError("during seedSet requires 1-30 unique integers in [0, 2147483647]")
    scenario.update(presetVersion=preset["version"], presetFingerprint=preset_fingerprint(preset), duringPreset=preset)
    return scenario


def verify_bound_preset(scenario):
    preset = load_during_preset(scenario["presetId"], version=scenario["presetVersion"], fingerprint=scenario["presetFingerprint"])
    if scenario.get("duringPreset") != preset:
        raise ValueError("experiment during preset snapshot mismatch")
    return preset
