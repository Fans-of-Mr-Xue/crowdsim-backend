"""Plain-data contracts used at the DeepSeek boundary."""

from typing import Final, Literal, TypedDict


PedestrianAction = Literal[
    "continue",
    "slow_down",
    "wait",
    "reroute",
    "change_goal",
]
DecisionSource = Literal["llm", "local_fallback"]
DensityLevel = Literal["free", "busy", "crowded", "critical", "unknown"]

ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"continue", "slow_down", "wait", "reroute", "change_goal"}
)
DECISION_SOURCES: Final[frozenset[str]] = frozenset({"llm", "local_fallback"})
DENSITY_LEVELS: Final[frozenset[str]] = frozenset(
    {"free", "busy", "crowded", "critical", "unknown"}
)


class PedestrianProfile(TypedDict):
    nationality: str
    language: str
    age_group: str
    risk_tolerance: float
    crowding_tolerance: float
    familiarity: float
    patience: float


class PedestrianCurrentState(TypedDict):
    status: str
    speed: float
    stress: float
    fatigue: float
    flood_impact: float
    event_impact: float
    perceived_risk: float
    blocked_duration: float


class SurroundingCrowd(TypedDict):
    nearby_people: int
    local_density: float | None
    density_level: DensityLevel
    perceived_crowding: float


class RouteCandidateContext(TypedDict):
    target_id: str
    target_kind: str
    cost_seconds: float


class PedestrianDecisionContext(TypedDict):
    agent_id: str
    snapshot_id: str
    time_seconds: float
    profile: PedestrianProfile
    current_state: PedestrianCurrentState
    surrounding_crowd: SurroundingCrowd
    candidates: list[RouteCandidateContext]


class PedestrianDecisionResult(TypedDict):
    action: PedestrianAction
    target_id: str | None
    reason: str
    confidence: float
    source: DecisionSource
