import random
from typing import Any, Dict


PROFILES: Dict[str, Dict[str, Any]] = {
    "east_asian": dict(native_language="zh", desired_speed=(1.15, 1.45), personal_space=(0.4, 0.7), density_tolerance=(6.0, 8.0), barrier_compliance=(0.92, 0.99), queue_mode="strict_single", shortest_path_weight=(0.65, 0.85), following_tendency=(0.78, 0.95), language_delay=(0.5, 1.2), symbol_accuracy=(0.9, 0.99), authority_compliance=(0.9, 0.99), same_culture_attraction=(0.75, 0.95), group_cohesion=(0.82, 0.98), help_seeking=(0.2, 0.45), conflict_threshold=(8.0, 9.5), panic_susceptibility=(0.7, 0.9), stress_response="follow_guidance", counterflow_tendency=(0.3, 0.5), recovery_seconds=(30, 60)),
    "western": dict(native_language="en", desired_speed=(1.4, 1.6), personal_space=(0.7, 1.2), density_tolerance=(4.0, 5.5), barrier_compliance=(0.7, 0.9), queue_mode="strict_single", shortest_path_weight=(0.3, 0.55), following_tendency=(0.45, 0.65), language_delay=(1.0, 2.0), symbol_accuracy=(0.88, 0.98), authority_compliance=(0.65, 0.82), same_culture_attraction=(0.25, 0.5), group_cohesion=(0.45, 0.72), help_seeking=(0.7, 0.9), conflict_threshold=(7.0, 9.0), panic_susceptibility=(0.4, 0.6), stress_response="self_evacuate", counterflow_tendency=(0.03, 0.12), recovery_seconds=(30, 65)),
    "southeast_asian": dict(native_language="other", desired_speed=(1.0, 1.25), personal_space=(0.4, 0.75), density_tolerance=(5.5, 7.0), barrier_compliance=(0.85, 0.97), queue_mode="loose_cluster", shortest_path_weight=(0.65, 0.85), following_tendency=(0.72, 0.9), language_delay=(1.5, 3.0), symbol_accuracy=(0.78, 0.94), authority_compliance=(0.86, 0.96), same_culture_attraction=(0.78, 0.95), group_cohesion=(0.78, 0.95), help_seeking=(0.25, 0.5), conflict_threshold=(6.0, 8.0), panic_susceptibility=(0.65, 0.85), stress_response="follow_crowd", counterflow_tendency=(0.2, 0.4), recovery_seconds=(45, 90)),
    "other_collectivist": dict(native_language="other", desired_speed=(1.05, 1.4), personal_space=(0.4, 0.8), density_tolerance=(5.0, 7.0), barrier_compliance=(0.5, 0.8), queue_mode="loose_cluster", shortest_path_weight=(0.55, 0.8), following_tendency=(0.7, 0.92), language_delay=(2.0, 4.0), symbol_accuracy=(0.7, 0.9), authority_compliance=(0.5, 0.75), same_culture_attraction=(0.75, 0.95), group_cohesion=(0.88, 0.99), help_seeking=(0.45, 0.7), conflict_threshold=(4.5, 7.0), panic_susceptibility=(0.7, 0.9), stress_response="follow_crowd", counterflow_tendency=(0.3, 0.55), recovery_seconds=(60, 120)),
}


def sample_profile(rng: random.Random) -> Dict[str, Any]:
    name = rng.choices(list(PROFILES), weights=[0.70, 0.20, 0.05, 0.05])[0]
    source = PROFILES[name]
    result = {"cultural_group": name}
    for key, value in source.items():
        result[key] = rng.uniform(*value) if isinstance(value, tuple) else value
    result["low_density_path_weight"] = 1.0 - result["shortest_path_weight"]
    result["information_trust"] = {
        "official": min(5.0, 2.0 + 3.0 * result["authority_compliance"]),
        "companions": min(5.0, 2.0 + 3.0 * result["group_cohesion"]),
        "strangers": min(5.0, 1.5 + 3.0 * result["help_seeking"]),
        "social_media": rng.uniform(2.0, 4.5),
    }
    return result
