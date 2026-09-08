from typing import Dict


EVENT_PRESETS: Dict[str, Dict[str, float]] = {
    "generic": {"density": 1.8, "speed": 0.35, "stress": 0.60, "avoidance": 0.50},
    "fire": {"density": 2.2, "speed": 0.55, "stress": 0.95, "avoidance": 0.95},
    "smoke": {"density": 1.5, "speed": 0.65, "stress": 0.80, "avoidance": 0.90},
    "rumor": {"density": 2.4, "speed": 0.15, "stress": 0.90, "avoidance": 0.65},
    "alarm": {"density": 1.7, "speed": 0.20, "stress": 0.75, "avoidance": 0.60},
    "obstacle": {"density": 2.0, "speed": 0.80, "stress": 0.35, "avoidance": 0.85},
    "flood": {"density": 1.4, "speed": 0.75, "stress": 0.45, "avoidance": 0.80},
}


def event_defaults(event_type: str) -> Dict[str, float]:
    return EVENT_PRESETS.get(event_type, EVENT_PRESETS["generic"]).copy()
