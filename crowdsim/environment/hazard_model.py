"""External hazard inputs and explicit per-person movement constraints."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict, Iterable

from crowdsim.domain.crowdsim_models import MotionSnapshot


@dataclass
class HazardZone:
    hazard_id: str
    hazard_type: str
    x: float
    y: float
    radius: float
    intensity: float
    speed_reduction: float
    expires_at: float | None = None
    lon: float | None = None
    lat: float | None = None


class HazardModel:
    def __init__(self) -> None:
        self.zones: Dict[str, HazardZone] = {}

    def update(self, now: float) -> None:
        self.zones = {hazard_id: zone for hazard_id, zone in self.zones.items() if zone.expires_at is None or now < zone.expires_at}

    def replace_static_points(self, zones: Iterable[HazardZone]) -> None:
        self.zones = {zone.hazard_id: zone for zone in zones}

    def replace_type(self, hazard_type: str, zones: Iterable[HazardZone]) -> None:
        self.zones = {hazard_id: zone for hazard_id, zone in self.zones.items() if zone.hazard_type != hazard_type}
        self.zones.update({zone.hazard_id: zone for zone in zones})

    def add(self, zone: HazardZone) -> None:
        self.zones[zone.hazard_id] = zone

    def impact_for(self, motion: MotionSnapshot) -> float:
        impact = 0.0
        for zone in self.zones.values():
            distance = math.hypot(motion.x - zone.x, motion.y - zone.y)
            if distance <= zone.radius:
                impact = max(impact, zone.intensity * (1.0 - distance / max(0.01, zone.radius)))
        return max(0.0, min(1.0, impact))

    def speed_limit_for(self, motion: MotionSnapshot, base_speed: float) -> float | None:
        limit = None
        for zone in self.zones.values():
            distance = math.hypot(motion.x - zone.x, motion.y - zone.y)
            if distance <= zone.radius:
                local = zone.intensity * (1.0 - distance / max(0.01, zone.radius))
                candidate = base_speed * max(0.05, 1.0 - local * zone.speed_reduction)
                limit = candidate if limit is None else min(limit, candidate)
        return limit

    def serialized_points(self) -> list[dict]:
        return [{"id": zone.hazard_id, "lng": zone.lon, "lat": zone.lat, "depth": zone.intensity, "radius": zone.radius, "hazard_type": zone.hazard_type} for zone in self.zones.values() if zone.lon is not None and zone.lat is not None]
