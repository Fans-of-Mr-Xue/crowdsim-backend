"""Display-only crowd indicators; never feed these states into movement decisions."""

from dataclasses import asdict, dataclass, fields
import json
import math
from pathlib import Path


STATE_COLORS = {
    "normal": "green", "busy": "yellow", "planned_stop": "blue",
    "crowded": "orange", "high_risk": "red",
}


@dataclass(frozen=True)
class CrowdVisualPolicy:
    busy_density_person_per_m2: float = 0.5
    crowded_density_person_per_m2: float = 1.5
    stalled_high_risk_density_person_per_m2: float = 3.5
    critical_density_person_per_m2: float = 4.0
    low_speed_threshold_mps: float = 0.2
    confirmation_duration_seconds: float = 5.0

    def __post_init__(self):
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value <= 0 for value in asdict(self).values()):
            raise ValueError("crowd visual thresholds must be finite positive numbers")
        levels = (self.busy_density_person_per_m2, self.crowded_density_person_per_m2,
                  self.stalled_high_risk_density_person_per_m2, self.critical_density_person_per_m2)
        if any(a >= b for a, b in zip(levels, levels[1:])):
            raise ValueError("crowd visual density thresholds must be strictly increasing")

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else Path(__file__).resolve().parents[2] / "config/crowd_visual_state.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{field.name: payload[field.name] for field in fields(cls)})

    def metadata(self):
        return {
            "version": 1,
            "thresholds": asdict(self),
            "state_colors": dict(STATE_COLORS),
            "parameter_status": "engineering_assumptions_not_injury_prediction",
            "density_method": "same_edge_personal_radius_area_approximation",
        }

    @staticmethod
    def valid_density(density):
        return density is not None and math.isfinite(density) and density >= 0

    @staticmethod
    def planned_stop(state, now):
        return state.activity_state == "hotspot_dwelling" or (
            state.planned_wait_until is not None and state.planned_wait_until > now
        )

    def durations(self, old, observation, dt):
        density = observation.objective_density_per_m2
        low = observation.own_motion.speed < self.low_speed_threshold_mps
        valid = self.valid_density(density)
        dt = max(0.0, dt)
        return {
            "low_speed_duration": old.low_speed_duration + dt if low else 0.0,
            "critical_density_duration": old.critical_density_duration + dt
                if valid and density >= self.critical_density_person_per_m2 else 0.0,
            # The TWO conditions must hold concurrently for the full interval.
            "dense_low_speed_duration": old.dense_low_speed_duration + dt
                if valid and low and density >= self.stalled_high_risk_density_person_per_m2 else 0.0,
            "visual_blocked_duration": old.visual_blocked_duration + dt
                if low and not self.planned_stop(old, observation.time_seconds) else 0.0,
        }

    def classify(self, motion, observation, state):
        density = observation.objective_density_per_m2 if observation else None
        valid = self.valid_density(density)
        sustained = self.confirmation_duration_seconds
        if valid and density >= self.critical_density_person_per_m2 and state.critical_density_duration + 1e-9 >= sustained:
            name, reason = "high_risk", "sustained_critical_density"
        elif (valid and density >= self.stalled_high_risk_density_person_per_m2
              and motion.speed < self.low_speed_threshold_mps
              and state.dense_low_speed_duration + 1e-9 >= sustained):
            name, reason = "high_risk", "sustained_dense_low_speed"
        elif valid and density >= self.crowded_density_person_per_m2:
            name, reason = "crowded", "crowded_density"
        elif self.planned_stop(state, motion.time_seconds):
            name, reason = "planned_stop", "planned_activity"
        elif (motion.speed < self.low_speed_threshold_mps
              and state.visual_blocked_duration + 1e-9 >= sustained):
            name, reason = "busy", "sustained_unplanned_low_speed"
        elif valid and density >= self.busy_density_person_per_m2:
            name, reason = "busy", "busy_density"
        else:
            name, reason = "normal", "below_warning_thresholds" if valid else "density_unavailable"
        return {
            "visual_state": name, "color": STATE_COLORS[name], "visual_reason": reason,
            "density_person_per_m2": density if valid else None,
            "density_valid": valid,
            "low_speed_duration_seconds": round(state.low_speed_duration, 3),
            "blocked_duration_seconds": round(state.blocked_duration, 3),
            "visual_blocked_duration_seconds": round(state.visual_blocked_duration, 3),
            "critical_density_duration_seconds": round(state.critical_density_duration, 3),
            "dense_low_speed_duration_seconds": round(state.dense_low_speed_duration, 3),
        }
