"""Version 1 input and output contracts for the pedestrian decision skill.

These definitions provide static typing and shared vocabulary. Runtime input
normalization is implemented by :func:`pedestrian_decision_skill.normalize_context`.
"""

from typing import Final, Literal, TypedDict


PedestrianAction = Literal[
    "continue",
    "slow_down",
    "avoid",
    "follow_crowd",
    "wait",
]
DecisionSource = Literal["llm", "local_fallback"]
DensityLevel = Literal["free", "busy", "crowded", "critical"]

ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"continue", "slow_down", "avoid", "follow_crowd", "wait"}
)
DECISION_SOURCES: Final[frozenset[str]] = frozenset({"llm", "local_fallback"})
DENSITY_LEVELS: Final[frozenset[str]] = frozenset(
    {"free", "busy", "crowded", "critical"}
)


class PedestrianProfile(TypedDict, total=False):
    """Stable identity information supplied by the simulator adapter."""

    nationality: str
    language: str


class PedestrianCurrentState(TypedDict, total=False):
    """Dynamic state at the moment a decision is requested."""

    status: str
    speed: float
    stress: float
    fatigue: float
    flood_impact: float
    event_impact: float


class SurroundingCrowd(TypedDict, total=False):
    """Aggregated local-crowd information; never a raw neighbour list."""

    nearby_people: int
    local_density: float
    density_level: DensityLevel


class PedestrianDecisionContext(TypedDict):
    """Plain-dictionary input accepted by the standalone skill."""

    agent_id: str
    profile: PedestrianProfile
    current_state: PedestrianCurrentState
    surrounding_crowd: SurroundingCrowd


class PedestrianDecisionResult(TypedDict):
    """Standardized result returned to the simulator adapter."""

    action: PedestrianAction
    reason: str
    confidence: float
    source: DecisionSource
