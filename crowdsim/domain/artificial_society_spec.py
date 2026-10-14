"""Validation and capability contract for published artificial-society models."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from crowdsim.modeling_algorithm.registry import algorithm_catalog


SCHEMA_VERSION = 2
REQUIRED_ROLE_IDS = {"pedestrian", "signal", "policy"}
REQUIRED_LAYER_IDS = {"topology", "dynamic", "information", "intervention"}
REQUIRED_RULE_IDS = {
    "population-allocation", "sumo-motion", "local-observation",
    "decision-scheduling", "visual-state", "message-control",
}


class ArtificialSocietyValidationError(ValueError):
    """User-facing validation error for a manual artificial-society model."""


@dataclass(frozen=True)
class ArtificialSocietySpec:
    payload: dict[str, Any]
    fingerprint: str

    @property
    def requirement_id(self) -> str:
        return str(self.payload["requirement_id"])

    @classmethod
    def parse(cls, raw: Any) -> "ArtificialSocietySpec":
        if not isinstance(raw, dict):
            raise ArtificialSocietyValidationError("artificial_society must be an object")
        payload = deepcopy(raw)
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ArtificialSocietyValidationError(f"schema_version must be {SCHEMA_VERSION}")
        if payload.get("source") != "manual":
            raise ArtificialSocietyValidationError("source must be manual")

        requirement_id = payload.get("requirement_id")
        if not isinstance(requirement_id, str) or not requirement_id:
            raise ArtificialSocietyValidationError("requirement_id is required")
        if not isinstance(payload.get("requirement_fingerprint"), str):
            raise ArtificialSocietyValidationError("requirement_fingerprint must be a string")

        individual = _object(payload, "individual_model")
        environment = _object(payload, "environment_model")
        rules = _object(payload, "rule_model")
        _object(payload, "overview")

        total = individual.get("population_total")
        if type(total) is not int or total < 1:
            raise ArtificialSocietyValidationError("individual_model.population_total must be a positive integer")
        distributions = _object(individual, "distributions")
        missing = {"crowd_role", "age_band", "origin", "gender"} - set(distributions)
        if missing:
            raise ArtificialSocietyValidationError(
                f"individual_model.distributions is missing: {', '.join(sorted(missing))}"
            )
        for name in {"crowd_role", "age_band", "origin", "gender"}:
            _validate_distribution(name, distributions[name])

        _validate_selected_ids(individual.get("roles"), REQUIRED_ROLE_IDS, "individual_model.roles")
        _validate_profile_parameters(individual.get("profile_parameters"))
        _validate_agent_types(individual.get("agent_types"), distributions["crowd_role"])
        _validate_selected_ids(environment.get("layers"), REQUIRED_LAYER_IDS, "environment_model.layers")
        _validate_environment_structure(environment.get("spatial_structure"))
        _validate_selected_ids(rules.get("rules"), REQUIRED_RULE_IDS, "rule_model.rules")
        _validate_runtime_parameters(rules.get("runtime_parameters"))

        binding = _object(environment, "scenario_binding")
        if not isinstance(binding.get("location_id"), str) or not binding["location_id"]:
            raise ArtificialSocietyValidationError("environment_model.scenario_binding.location_id is required")

        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(payload=payload, fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    def capabilities(self) -> dict[str, Any]:
        """State exactly which model fields the current runtime consumes."""
        return {
            "population_total": "requirement_runtime_consumed",
            "population_distributions": "requirement_runtime_consumed",
            "profile_parameters": "runtime_consumed",
            "agent_types": "runtime_consumed",
            "agent_capabilities": "runtime_consumed_for_implemented_chain",
            "agent_module_methods": "runtime_consumed_for_implemented_chain",
            "implemented_agent_chain": {
                "emotion": "PAD 连续情感模型",
                "desire": "情感驱动欲望",
                "game": "有限理性博弈",
                "decision": "LLM-driven",
                "learning": "经验回放",
            },
            "environment_structure": "published_environment_contract",
            "environment_factors": "published_environment_contract",
            "environment_responses": "published_environment_contract",
            "decision_period_seconds": "runtime_consumed",
            "visual_state_thresholds": "runtime_consumed",
            "scenario_binding": "validated_against_requirement",
            "roles": {
                "pedestrian": "runtime_component",
                "signal": "runtime_component",
                "policy": "runtime_component",
            },
            "environment_layers": "runtime_components",
            "rules": "runtime_components",
            "descriptive_fields": "metadata_only",
        }


def _object(parent: dict, key: str) -> dict:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ArtificialSocietyValidationError(f"{key} must be an object")
    return value


def _validate_distribution(name: str, rows: Any) -> None:
    if not isinstance(rows, list) or not rows:
        raise ArtificialSocietyValidationError(f"distribution {name} must not be empty")
    total = 0.0
    for item in rows:
        if not isinstance(item, dict):
            raise ArtificialSocietyValidationError(f"distribution {name} rows must be objects")
        percent = item.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or percent < 0:
            raise ArtificialSocietyValidationError(f"distribution {name} percent must be non-negative")
        total += float(percent)
    if abs(total - 100.0) > 1e-6:
        raise ArtificialSocietyValidationError(f"distribution {name} must total 100")


def _validate_profile_parameters(value: Any) -> None:
    if not isinstance(value, dict) or not value:
        raise ArtificialSocietyValidationError("individual_model.profile_parameters must not be empty")
    supported = {
        "free_walking_speed", "perception_radius", "crowding_tolerance",
        "following_tendency", "authority_compliance",
    }
    unknown = set(value) - supported
    if unknown:
        raise ArtificialSocietyValidationError(
            f"unsupported profile parameters: {', '.join(sorted(unknown))}"
        )
    for name, parameter in value.items():
        if not isinstance(parameter, dict):
            raise ArtificialSocietyValidationError(f"profile parameter {name} must be an object")
        minimum, maximum = parameter.get("min"), parameter.get("max")
        floor, ceiling = parameter.get("floor"), parameter.get("ceiling")
        if any(isinstance(number, bool) or not isinstance(number, (int, float))
               for number in (minimum, maximum, floor, ceiling)):
            raise ArtificialSocietyValidationError(f"profile parameter {name} has invalid bounds")
        if not floor <= minimum <= maximum <= ceiling:
            raise ArtificialSocietyValidationError(f"profile parameter {name} is outside allowed bounds")


def _validate_agent_types(value: Any, crowd_roles: list[dict[str, Any]]) -> None:
    if not isinstance(value, list) or not value:
        raise ArtificialSocietyValidationError("individual_model.agent_types must not be empty")
    expected_ids = {str(item.get("code") or item.get("id")) for item in crowd_roles}
    actual_ids = {str(item.get("id")) for item in value if isinstance(item, dict)}
    if actual_ids != expected_ids:
        raise ArtificialSocietyValidationError("individual_model.agent_types must match crowd_role distribution")
    required_modules = {"perception", "goal", "decision", "action"}
    for agent in value:
        if not isinstance(agent, dict) or not str(agent.get("name") or "").strip():
            raise ArtificialSocietyValidationError("agent type name is required")
        capabilities = agent.get("capabilities")
        if not isinstance(capabilities, dict):
            raise ArtificialSocietyValidationError("agent capabilities must be an object")
        architecture = agent.get("architecture")
        if not isinstance(architecture, list) or not architecture:
            raise ArtificialSocietyValidationError("agent architecture must not be empty")
        selected = [item for item in architecture if isinstance(item, dict) and item.get("selected") is not False]
        module_ids = [str(item.get("id")) for item in selected]
        if len(module_ids) != len(set(module_ids)):
            raise ArtificialSocietyValidationError("agent architecture module ids must be unique")
        missing = required_modules - set(module_ids)
        if missing:
            raise ArtificialSocietyValidationError(f"agent architecture is missing: {', '.join(sorted(missing))}")
        if capabilities.get("emotion") and not {"emotion", "desire"}.issubset(module_ids):
            raise ArtificialSocietyValidationError("emotion capability requires emotion and desire modules")
        if capabilities.get("game") and "game" not in module_ids:
            raise ArtificialSocietyValidationError("game capability requires game module")
        if capabilities.get("learning") and "learning" not in module_ids:
            raise ArtificialSocietyValidationError("learning capability requires learning module")
        if any(not str(item.get("method") or "").strip() for item in selected):
            raise ArtificialSocietyValidationError("agent architecture methods are required")
        supported_methods: dict[str, set[str]] = {}
        for registered in algorithm_catalog():
            if registered["model"] == "individual":
                supported_methods.setdefault(str(registered["module"]), set()).add(str(registered["method"]))
        for item in selected:
            module_id = str(item.get("id") or "")
            method = str(item.get("method") or "")
            if method not in supported_methods.get(module_id, set()):
                raise ArtificialSocietyValidationError(
                    f"unsupported agent method: {module_id}.{method}"
                )
        attributes = agent.get("attributes")
        if not isinstance(attributes, dict) or not attributes:
            raise ArtificialSocietyValidationError("agent attributes must not be empty")
        for attribute in attributes.values():
            if not isinstance(attribute, dict):
                raise ArtificialSocietyValidationError("agent attribute must be an object")
            minimum, maximum = attribute.get("min"), attribute.get("max")
            if any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in (minimum, maximum)):
                raise ArtificialSocietyValidationError("agent attribute bounds must be numeric")
            if minimum > maximum:
                raise ArtificialSocietyValidationError("agent attribute minimum must not exceed maximum")


def _validate_environment_structure(value: Any) -> None:
    if not isinstance(value, dict):
        raise ArtificialSocietyValidationError("environment_model.spatial_structure must be an object")
    if not str(value.get("network_name") or "").strip() or not str(value.get("network_method") or "").strip():
        raise ArtificialSocietyValidationError("environment network name and method are required")
    for key in ("entrances", "exits"):
        rows = value.get(key)
        if not isinstance(rows, list) or not rows:
            raise ArtificialSocietyValidationError(f"environment {key} must not be empty")
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("name") or "").strip():
                raise ArtificialSocietyValidationError(f"environment {key} names are required")
            capacity = row.get("capacity_per_minute")
            if isinstance(capacity, bool) or not isinstance(capacity, (int, float)) or capacity < 0:
                raise ArtificialSocietyValidationError(f"environment {key} capacity must be non-negative")
    factors = value.get("factors")
    if not isinstance(factors, list) or not any(isinstance(item, dict) and item.get("selected") is not False for item in factors):
        raise ArtificialSocietyValidationError("at least one environment factor is required")
    responses = value.get("responses")
    selected = [item for item in responses or [] if isinstance(item, dict) and item.get("selected") is not False]
    if not selected:
        raise ArtificialSocietyValidationError("at least one environment response is required")
    for item in selected:
        if not str(item.get("theory") or "").strip() or not str(item.get("method") or "").strip():
            raise ArtificialSocietyValidationError("environment response theory and method are required")


def _validate_runtime_parameters(value: Any) -> None:
    if not isinstance(value, dict):
        raise ArtificialSocietyValidationError("rule_model.runtime_parameters must be an object")
    period = value.get("decision_period_seconds")
    if isinstance(period, bool) or not isinstance(period, (int, float)) or period <= 0:
        raise ArtificialSocietyValidationError("decision_period_seconds must be positive")
    visual = value.get("visual_state")
    if not isinstance(visual, dict):
        raise ArtificialSocietyValidationError("runtime visual_state must be an object")
    required = {
        "busy_density_person_per_m2", "crowded_density_person_per_m2",
        "stalled_high_risk_density_person_per_m2", "critical_density_person_per_m2",
        "low_speed_threshold_mps", "confirmation_duration_seconds",
    }
    if required - set(visual):
        raise ArtificialSocietyValidationError("runtime visual_state fields are incomplete")
    if any(isinstance(visual[name], bool) or not isinstance(visual[name], (int, float))
           or visual[name] <= 0 for name in required):
        raise ArtificialSocietyValidationError("runtime visual_state values must be positive numbers")
    levels = [
        visual["busy_density_person_per_m2"],
        visual["crowded_density_person_per_m2"],
        visual["stalled_high_risk_density_person_per_m2"],
        visual["critical_density_person_per_m2"],
    ]
    if any(left >= right for left, right in zip(levels, levels[1:])):
        raise ArtificialSocietyValidationError("runtime visual_state density thresholds must be strictly increasing")


def _validate_selected_ids(rows: Any, required: set[str], path: str) -> None:
    if not isinstance(rows, list):
        raise ArtificialSocietyValidationError(f"{path} must be an array")
    selected = {
        str(item.get("id")) for item in rows
        if isinstance(item, dict) and item.get("selected") is not False
    }
    missing = required - selected
    if missing:
        raise ArtificialSocietyValidationError(f"{path} is missing: {', '.join(sorted(missing))}")
