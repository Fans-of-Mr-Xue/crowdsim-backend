import math
from typing import Any, Dict, List

from crowdsim_models import CrowdEvent
from event_catalog import event_defaults


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class EventManager:
    """Owns generic emergency-event creation, evolution, and removal."""

    def __init__(self, network: Any) -> None:
        self.network = network
        self.events: List[CrowdEvent] = []

    def apply(self, data: Dict[str, Any], now: float, step_index: int) -> None:
        if data.get("mode") == "clear":
            event_id = data.get("id")
            self.events = [] if not event_id else [event for event in self.events if event.id != event_id]
            return
        lon = _number(data.get("lng", data.get("lon")), math.nan)
        lat = _number(data.get("lat"), math.nan)
        if not math.isfinite(lon) or not math.isfinite(lat):
            return
        event_type = str(data.get("eventType", "generic"))
        defaults = event_defaults(event_type)
        effects = data.get("effects") if isinstance(data.get("effects"), dict) else {}
        duration = max(1.0, _number(data.get("duration"), 60.0))
        growth = max(0.0, min(duration, _number(data.get("growthSeconds"), 0.0)))
        decay = max(0.0, min(duration - growth, _number(data.get("decaySeconds"), 0.0)))
        peak = max(0.0, min(1.0, _number(data.get("intensity"), 0.7)))
        x, y = self.network.lonlat_to_xy(lon, lat)
        self.events.append(CrowdEvent(
            id=str(data.get("id") or f"event_{step_index}_{len(self.events)}"),
            x=x, y=y,
            radius=max(5.0, _number(data.get("radius"), 40.0)),
            intensity=0.0 if growth else peak,
            peak_intensity=peak,
            density_multiplier=max(0.1, min(5.0, _number(data.get("densityMultiplier"), defaults["density"]))),
            expires_at=now + duration,
            started_at=now,
            growth_seconds=growth,
            decay_seconds=decay,
            event_type=event_type,
            speed_impact=max(0.0, min(1.0, _number(effects.get("speed", data.get("speedImpact")), defaults["speed"]))),
            stress_impact=max(0.0, min(1.0, _number(effects.get("stress", data.get("stressImpact")), defaults["stress"]))),
            avoidance_pressure=max(0.0, min(1.0, _number(effects.get("avoidance", data.get("avoidancePressure")), defaults["avoidance"]))),
            source=str(data.get("source", "event")),
            phase="growing" if growth else "active",
        ))

    def step(self, now: float) -> None:
        active = []
        for event in self.events:
            if now >= event.expires_at:
                continue
            elapsed = now - event.started_at
            remaining = event.expires_at - now
            if event.growth_seconds and elapsed < event.growth_seconds:
                event.phase = "growing"
                event.intensity = event.peak_intensity * elapsed / event.growth_seconds
            elif event.decay_seconds and remaining <= event.decay_seconds:
                event.phase = "decaying"
                event.intensity = event.peak_intensity * remaining / event.decay_seconds
            else:
                event.phase = "active"
                event.intensity = event.peak_intensity
            active.append(event)
        self.events = active

    def mitigate(self, amount: float) -> None:
        """Reduce controllable event intensity after an emergency intervention."""
        if amount <= 0.0:
            return
        for event in self.events:
            event.peak_intensity = max(0.0, event.peak_intensity - amount)
            event.intensity = min(event.intensity, event.peak_intensity)
