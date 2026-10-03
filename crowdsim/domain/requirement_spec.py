"""Validated requirement-definition contract shared by WebSocket and runtime."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any


SCHEMA_VERSION = 1
MAX_POPULATION = 10_000
SUPPORTED_EVENT_IDS = {
    "culture", "sports", "commerce", "social-leisure", "public-social",
    "professional", "emergency",
}
SUPPORTED_METRIC_IDS = {
    "global-density", "local-density", "global-speed", "local-speed",
    "boundary-density-difference", "evacuation-time", "evacuation-efficiency",
    "absolute-evacuation-density", "pedestrian-state-change",
    "pedestrian-psychology-change", "concise-decision-advice",
    "detailed-decision-plan",
}
SUPPORTED_LOCATION_IDS = {"memorial-tower"}
REQUIRED_DISTRIBUTIONS = {"crowd_role", "age_band", "origin", "gender"}


class RequirementValidationError(ValueError):
    """A user-facing validation failure in a requirement payload."""


@dataclass(frozen=True)
class RequirementSpec:
    payload: dict[str, Any]
    fingerprint: str

    @property
    def population_total(self) -> int:
        return int(self.payload["population"]["total"])

    @property
    def location_id(self) -> str:
        return str(self.payload["spatial_scope"]["location_id"])

    @property
    def distributions(self) -> dict[str, list[dict[str, Any]]]:
        return self.payload["population"]["distributions"]

    @property
    def metric_ids(self) -> tuple[str, ...]:
        return tuple(self.payload["observation"]["metric_ids"])

    @classmethod
    def parse(cls, raw: Any) -> "RequirementSpec":
        if not isinstance(raw, dict):
            raise RequirementValidationError("requirement must be an object")
        payload = deepcopy(raw)
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise RequirementValidationError(f"schema_version must be {SCHEMA_VERSION}")

        project = _object(payload, "project")
        _text(project, "title", minimum=1, maximum=120)
        _text(project, "description", minimum=1, maximum=2_000)

        spatial = _object(payload, "spatial_scope")
        source = _text(spatial, "source", minimum=1, maximum=20)
        if source not in {"preset", "custom"}:
            raise RequirementValidationError("spatial_scope.source must be preset or custom")
        _text(spatial, "location_id", minimum=1, maximum=80)
        _text(spatial, "name", minimum=1, maximum=120)
        if spatial.get("crs") != "EPSG:4326":
            raise RequirementValidationError("spatial_scope.crs must be EPSG:4326")
        _coordinate(spatial.get("center"), "spatial_scope.center")
        _polygon(spatial.get("boundary"))

        population = _object(payload, "population")
        total = population.get("total")
        if type(total) is not int or not 1 <= total <= MAX_POPULATION:
            raise RequirementValidationError(
                f"population.total must be an integer between 1 and {MAX_POPULATION}"
            )
        distributions = _object(population, "distributions")
        missing = REQUIRED_DISTRIBUTIONS - set(distributions)
        if missing:
            raise RequirementValidationError(
                f"missing population distributions: {', '.join(sorted(missing))}"
            )
        for name in REQUIRED_DISTRIBUTIONS:
            _distribution(name, distributions[name])

        scenario = _object(payload, "scenario")
        event = _object(scenario, "event_category")
        event_id = _text(event, "id", minimum=1, maximum=80)
        if event_id not in SUPPORTED_EVENT_IDS:
            raise RequirementValidationError(f"unsupported event category: {event_id}")

        observation = _object(payload, "observation")
        metric_ids = observation.get("metric_ids")
        if not isinstance(metric_ids, list) or not metric_ids:
            raise RequirementValidationError("observation.metric_ids must not be empty")
        if len(metric_ids) != len(set(metric_ids)):
            raise RequirementValidationError("observation.metric_ids contains duplicates")
        unknown_metrics = set(metric_ids) - SUPPORTED_METRIC_IDS
        if unknown_metrics:
            raise RequirementValidationError(
                f"unsupported observation metrics: {', '.join(sorted(unknown_metrics))}"
            )
        partition = observation.get("local_partition_mode", "uniform")
        if partition not in {"uniform", "random"}:
            raise RequirementValidationError(
                "observation.local_partition_mode must be uniform or random"
            )
        observation["local_partition_mode"] = partition

        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(payload=payload, fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    def capabilities(self) -> dict[str, Any]:
        location_supported = self.location_id in SUPPORTED_LOCATION_IDS
        implemented_metrics = {"global-speed", "local-density", "local-speed"}
        partial_metrics = {
            "global-density", "evacuation-time", "absolute-evacuation-density",
            "pedestrian-state-change", "pedestrian-psychology-change",
        }
        metric_status = {
            metric_id: (
                "supported" if metric_id in implemented_metrics
                else "partial" if metric_id in partial_metrics
                else "not_implemented"
            )
            for metric_id in self.metric_ids
        }
        warnings = []
        if not location_supported:
            warnings.append(
                f"地点 {self.location_id} 已保存，但当前 SUMO 场景尚不能据此初始化"
            )
        unavailable = [name for name, status in metric_status.items() if status == "not_implemented"]
        if unavailable:
            warnings.append(f"部分指标尚未实现：{', '.join(unavailable)}")
        return {
            "location": "supported" if location_supported else "not_implemented",
            "population_total": "supported",
            "population_distributions": "supported",
            "event_category": "metadata_only",
            "metrics": metric_status,
            "warnings": warnings,
        }


def requirement_runtime_summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the accepted requirement fields exposed once in the init frame."""
    if not isinstance(record, dict):
        return None
    requirement = record.get("requirement")
    if not isinstance(requirement, dict):
        return None
    return {
        "requirement_id": record.get("requirement_id"),
        "schema_version": record.get("schema_version"),
        "fingerprint": record.get("fingerprint"),
        "project": requirement.get("project", {}),
        "spatial_scope": requirement.get("spatial_scope", {}),
        "population": requirement.get("population", {}),
        "scenario": requirement.get("scenario", {}),
        "observation": requirement.get("observation", {}),
        "project_title": requirement.get("project", {}).get("title"),
        "location_id": requirement.get("spatial_scope", {}).get("location_id"),
        "population_total": requirement.get("population", {}).get("total"),
        "event_category_id": requirement.get("scenario", {}).get("event_category", {}).get("id"),
        "metric_ids": requirement.get("observation", {}).get("metric_ids", []),
        "capabilities": record.get("capabilities", {}),
    }


def _object(container: dict[str, Any], name: str) -> dict[str, Any]:
    value = container.get(name)
    if not isinstance(value, dict):
        raise RequirementValidationError(f"{name} must be an object")
    return value


def _text(container: dict[str, Any], name: str, *, minimum: int, maximum: int) -> str:
    value = container.get(name)
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise RequirementValidationError(
            f"{name} must contain between {minimum} and {maximum} characters"
        )
    container[name] = value.strip()
    return container[name]


def _coordinate(value: Any, field: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise RequirementValidationError(f"{field} must be [longitude, latitude]")
    longitude, latitude = value
    if not all(isinstance(item, (int, float)) and math.isfinite(item) for item in value):
        raise RequirementValidationError(f"{field} must contain finite numbers")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise RequirementValidationError(f"{field} is outside WGS84 bounds")
    return float(longitude), float(latitude)


def _polygon(value: Any) -> None:
    if not isinstance(value, dict) or value.get("type") != "Polygon":
        raise RequirementValidationError("spatial_scope.boundary must be a GeoJSON Polygon")
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) != 1:
        raise RequirementValidationError("only a single polygon ring is supported")
    ring = coordinates[0]
    if not isinstance(ring, list) or not 4 <= len(ring) <= 2_001:
        raise RequirementValidationError("polygon ring must contain 3 to 2000 points plus closure")
    normalized = [list(_coordinate(point, "spatial_scope.boundary coordinate")) for point in ring]
    if normalized[0] != normalized[-1]:
        raise RequirementValidationError("polygon ring must be closed")
    if len({tuple(point) for point in normalized[:-1]}) < 3:
        raise RequirementValidationError("polygon must contain at least 3 distinct points")
    value["coordinates"] = [normalized]


def _distribution(name: str, value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise RequirementValidationError(f"distribution {name} must not be empty")
    total = 0.0
    identities = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RequirementValidationError(f"distribution {name}[{index}] must be an object")
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            raise RequirementValidationError(f"distribution {name}[{index}].label is required")
        item["label"] = label.strip()
        identity = str(item.get("code") or item.get("id") or item["label"]).strip()
        if not identity or identity in identities:
            raise RequirementValidationError(f"distribution {name} contains duplicate items")
        identities.add(identity)
        percent = item.get("percent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool) or not math.isfinite(percent):
            raise RequirementValidationError(f"distribution {name}[{index}].percent must be numeric")
        if not 0 <= percent <= 100:
            raise RequirementValidationError(f"distribution {name}[{index}].percent is outside 0..100")
        total += float(percent)
    if abs(total - 100.0) >= 0.05:
        raise RequirementValidationError(f"distribution {name} must total 100%")
