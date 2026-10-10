"""Versioned observation policy; these engineering labels never drive movement."""

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path


OBSERVATION_VERSION = 2
EVACUATION_METRIC_IDS = (
    "evacuation-time", "evacuation-efficiency", "absolute-evacuation-density",
)
IMPLEMENTED_METRIC_IDS = (
    "global-density", "local-density", "global-speed", "local-speed",
    "boundary-density-difference", "pedestrian-state-change",
    "pedestrian-psychology-change",
) + EVACUATION_METRIC_IDS
DEFERRED_METRIC_IDS = (
    "concise-decision-advice", "detailed-decision-plan",
)
BEHAVIOR_STATES = ("walking", "waiting", "blocked", "avoiding")
PSYCHOLOGY_STATES = ("calm", "tense", "panic", "unknown")


@dataclass(frozen=True)
class ObservationConfig:
    grid_size_m: float = 20.0
    boundary_band_width_m: float = 5.0
    low_speed_threshold_mps: float = 0.2
    blocked_confirmation_seconds: float = 5.0
    crowded_density_person_per_m2: float = 1.5
    avoidance_risk_threshold: float = 0.35
    avoidance_duration_seconds: float = 5.0
    stress_tense_threshold: float = 0.4
    stress_panic_threshold: float = 0.7
    stress_hysteresis: float = 0.05
    psychology_confirmation_seconds: float = 5.0
    max_grid_cells: int = 10000

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"observation {name} must be finite numeric data")
            if value < 0 or (name not in {"stress_hysteresis", "psychology_confirmation_seconds"} and value == 0):
                raise ValueError(f"invalid observation {name}")
        if type(self.max_grid_cells) is not int:
            raise ValueError("max_grid_cells must be an integer")
        if not 0 < self.stress_tense_threshold < self.stress_panic_threshold < 1:
            raise ValueError("stress thresholds must be ordered within (0, 1)")
        if self.stress_hysteresis >= min(
            self.stress_tense_threshold, 1 - self.stress_panic_threshold,
            (self.stress_panic_threshold - self.stress_tense_threshold) / 2,
        ):
            raise ValueError("stress hysteresis is too large")
        if not 0 < self.avoidance_risk_threshold <= 1:
            raise ValueError("avoidance risk threshold must be within (0, 1]")
        if self.boundary_band_width_m > self.grid_size_m:
            raise ValueError("boundary band width must not exceed grid size")

    @classmethod
    def load(cls, path=None):
        source = Path(path) if path else Path(__file__).resolve().parents[2] / "config/observation_metrics.json"
        return cls(**json.loads(source.read_text(encoding="utf-8")))

    def metadata(self):
        return {
            "schema_version": OBSERVATION_VERSION,
            "partition_mode": "uniform",
            "density_area_basis": "selected_polygon_area",
            "speed_basis": "all_in_scope_pedestrians_including_stops",
            "crowding_basis": "existing_personal_density_approximation",
            "psychology_basis": "model_stress_with_hysteresis_and_confirmation",
            "parameter_status": "engineering_classification",
            "evacuation_basis": "all_run_pedestrians_normally_arrived_in_sumo",
            "evacuation_start_basis": "first_applied_police_guidance_or_temporary_diversion",
            "evacuation_efficiency_formula": "(density_at_strategy_application - density_at_completion) / evacuation_time",
            "density_difference_basis": "pairwise_absolute_difference_of_same_uniform_cells",
            "parameters": asdict(self),
            "units": {"area": "m2", "density": "person/m2", "speed": "m/s", "time": "simulation_s", "psychology": "0..1", "evacuation_efficiency": "person/(m2*simulation_s)"},
        }
