"""Shared contracts for artificial-society modeling algorithms."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PADState:
    pleasure: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0
    label: str = "neutral"

    def as_dict(self) -> dict[str, Any]:
        return {
            "pleasure": self.pleasure,
            "arousal": self.arousal,
            "dominance": self.dominance,
            "label": self.label,
        }


@dataclass(frozen=True)
class DesireState:
    scores: dict[str, float]
    primary: str

    def as_dict(self) -> dict[str, Any]:
        return {"scores": dict(self.scores), "primary": self.primary}


@dataclass(frozen=True)
class GameChoice:
    action: str
    target_id: str | None
    utility: float
    probabilities: dict[str, float]
    reason: str


@dataclass(frozen=True)
class Experience:
    person_id: str
    snapshot_id: str
    role_id: str
    emotion: dict[str, Any]
    desire: dict[str, Any]
    action: str
    target_id: str | None
    reward: float
    metadata: dict[str, Any] = field(default_factory=dict)
