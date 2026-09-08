from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class Segment:
    id: str
    from_node: str
    to_node: str
    points: List[Tuple[float, float]]
    length: float
    speed: float
    width: float
    pedestrian: bool
    vehicle: bool
    outgoing: List[str] = field(default_factory=list)
    density: float = 0.0
    congestion: float = 0.0
    water: float = 0.0


@dataclass
class RouteTemplate:
    id: str
    depart: float
    edges: List[str]


@dataclass
class FloodZone:
    lon: float
    lat: float
    x: float
    y: float
    depth: float
    radius: float


@dataclass
class CrowdEvent:
    id: str
    x: float
    y: float
    radius: float
    intensity: float
    density_multiplier: float
    expires_at: float
    event_type: str = "generic"
    speed_impact: float = 0.35
    stress_impact: float = 0.6
    avoidance_pressure: float = 0.5
    source: str = "event"
    started_at: float = 0.0
    peak_intensity: float = 1.0
    growth_seconds: float = 0.0
    decay_seconds: float = 0.0
    phase: str = "active"


@dataclass
class Agent:
    id: str
    kind: str
    route: List[str]
    route_index: int
    distance: float
    base_speed: float
    speed: float
    lateral: float
    color: str = "green"
    flood_impact: float = 0.0
    congestion: float = 0.0
    low_speed_ticks: int = 0
    age_group: str = "adult"
    mobility: float = 1.0
    risk_tolerance: float = 0.5
    familiarity: float = 0.5
    group_size: int = 1
    perception_radius: float = 12.0
    acceptable_people: int = 10
    nearby_people: int = 0
    local_density: float = 0.0
    density_level: str = "free"
    stress: float = 0.0
    fatigue: float = 0.0
    decision: str = "continue"
    decision_reason: str = "initial"
    next_decision_step: int = 0
    decision_source: str = "local"
    cultural_group: str = "east_asian"
    native_language: str = "zh"
    personal_space: float = 0.6
    density_tolerance: float = 6.0
    barrier_compliance: float = 0.9
    queue_mode: str = "strict_single"
    shortest_path_weight: float = 0.7
    low_density_path_weight: float = 0.3
    following_tendency: float = 0.7
    language_delay: float = 0.8
    symbol_accuracy: float = 0.9
    authority_compliance: float = 0.9
    information_trust: Dict[str, float] = field(default_factory=dict)
    same_culture_attraction: float = 0.7
    group_cohesion: float = 0.8
    help_seeking: float = 0.4
    conflict_threshold: float = 8.0
    panic_susceptibility: float = 0.7
    stress_response: str = "follow_guidance"
    counterflow_tendency: float = 0.2
    recovery_seconds: float = 60.0
    functional_zone: str = "tourism"
    event_phase: str = "gathering"
    event_impact: float = 0.0
    event_avoidance: float = 0.0
