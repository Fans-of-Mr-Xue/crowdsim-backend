"""Minimal normalization, prompt, and result-validation example."""

import json

from pedestrian_decision_skill import PedestrianDecisionSkill


def main() -> None:
    skill = PedestrianDecisionSkill()
    print("Allowed actions:", ", ".join(sorted(skill.ALLOWED_ACTIONS)))
    raw_context = {
        "agent_id": "demo-agent",
        "profile": {"nationality": "CN", "language": "zh"},
        "current_state": {"status": "normal", "stress": "0.2"},
        "surrounding_crowd": {"nearby_people": "3"},
    }
    normalized = skill.normalize_context(raw_context)
    print("Normalized context:", normalized)
    print(
        "DeepSeek messages:",
        json.dumps(skill.build_messages(raw_context), ensure_ascii=False, indent=2),
    )
    validated = skill.parse_decision(
        '{"action":"continue","reason":"环境安全","confidence":0.9}'
    )
    print("Validated decision:", validated)


if __name__ == "__main__":
    main()
