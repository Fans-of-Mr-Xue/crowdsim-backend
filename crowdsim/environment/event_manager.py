"""Emergency event lifecycle without virtual population or density multipliers."""

from __future__ import annotations

import math
from typing import Any, Dict, List

from crowdsim.domain.crowdsim_models import CrowdEvent
from crowdsim.environment.event_catalog import event_defaults


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class EventManager:
    def __init__(self, network: Any) -> None:
        self.network = network
        self.events: List[CrowdEvent] = []

    def apply(self, data: Dict[str, Any], now: float, step_index: int) -> CrowdEvent | None:
        if data.get("mode") == "clear":
            event_id = data.get("id")
            self.events = [] if not event_id else [event for event in self.events if event.id != event_id]
            return None
        lon = _number(data.get("lng", data.get("lon")), math.nan)
        lat = _number(data.get("lat"), math.nan)
        if not math.isfinite(lon) or not math.isfinite(lat):
            raise ValueError("event requires finite lng/lon and lat")
        event_type = str(data.get("eventType", "generic"))
        defaults = event_defaults(event_type)
        duration = max(1.0, _number(data.get("duration"), 60.0))
        growth = max(0.0, min(duration, _number(data.get("growthSeconds"), 0.0)))
        decay = max(0.0, min(duration - growth, _number(data.get("decaySeconds"), 0.0)))
        peak = max(0.0, min(1.0, _number(data.get("intensity"), 0.7)))
        x, y = self.network.lonlat_to_xy(lon, lat)
        event = CrowdEvent(id=str(data.get("id") or f"event_{step_index}_{len(self.events)}"), x=x, y=y, radius=max(1.0, _number(data.get("radius"), 40.0)), intensity=0.0 if growth else peak, density_multiplier=1.0, expires_at=now + duration, started_at=now, growth_seconds=growth, decay_seconds=decay, event_type=event_type, speed_impact=float(defaults["speed_reduction"]), stress_impact=float(defaults["risk"]), avoidance_pressure=float(defaults["risk"]), source=str(data.get("source", defaults["message_source"])), phase="growing" if growth else "active")
        self.events.append(event)
        return event

    def step(self, now: float) -> None:
        active = []
        for event in self.events:
            if now >= event.expires_at:
                continue
            elapsed, remaining = now - event.started_at, event.expires_at - now
            if event.growth_seconds and elapsed < event.growth_seconds:
                event.phase, event.intensity = "growing", event.peak_intensity * elapsed / event.growth_seconds
            elif event.decay_seconds and remaining <= event.decay_seconds:
                event.phase, event.intensity = "decaying", event.peak_intensity * remaining / event.decay_seconds
            else:
                event.phase, event.intensity = "active", event.peak_intensity
            active.append(event)
        self.events = active

    def serialize(self, now: float) -> list[dict]:
        return [{"id": event.id, "event_type": event.event_type, "phase": event.phase, "radius": event.radius, "intensity": event.intensity, "density_multiplier": 1.0, "density_multiplier_deprecated": True, "effects": {"speed": event.speed_impact, "stress": event.stress_impact, "avoidance": event.avoidance_pressure}, "source": event.source, "remaining_seconds": max(0.0, event.expires_at - now)} for event in self.events]
