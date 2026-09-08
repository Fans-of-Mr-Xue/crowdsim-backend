"""Event presets describe stimuli or hazards, never density outcomes."""

from __future__ import annotations

from typing import Any, Dict


EVENT_PRESETS: Dict[str, Dict[str, Any]] = {
    "generic": {"physical": True, "speed_reduction": 0.35, "risk": 0.6, "message_source": "official"},
    "fire": {"physical": True, "speed_reduction": 0.7, "risk": 0.95, "message_source": "official"},
    "smoke": {"physical": True, "speed_reduction": 0.6, "risk": 0.85, "message_source": "official"},
    "flood": {"physical": True, "speed_reduction": 0.75, "risk": 0.7, "message_source": "official"},
    "alarm": {"physical": False, "speed_reduction": 0.0, "risk": 0.65, "message_source": "official"},
    "rumor": {"physical": False, "speed_reduction": 0.0, "risk": 0.55, "message_source": "social_media"},
    "police_guidance": {"physical": False, "speed_reduction": 0.0, "risk": 0.0, "message_source": "official"},
    "temporary_diversion": {"physical": False, "speed_reduction": 0.0, "risk": 0.0, "message_source": "official"},
    "observe_only": {"physical": False, "speed_reduction": 0.0, "risk": 0.0, "message_source": "official"}
}


def event_defaults(event_type: str) -> Dict[str, Any]:
    return EVENT_PRESETS.get(event_type, EVENT_PRESETS["generic"]).copy()
