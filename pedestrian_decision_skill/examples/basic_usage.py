"""Build one frozen CrowdSim context without sending an API request."""

import json

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot, Observation
from pedestrian_decision_skill import PedestrianDecisionSkill


def main() -> None:
    profile = AgentProfile("demo-agent", nationality="CN", native_language="zh")
    state = AgentState("demo-agent", stress=0.2, fatigue=0.1)
    motion = MotionSnapshot("demo-agent", 1.0, 0, 0, 121.49, 31.24, 1.1, "edge", "edge_0", 0, 90, 0, 2)
    observation = Observation(
        "demo-agent",
        "demo-run:1",
        1.0,
        motion,
        local_people_count=4,
        objective_density_per_m2=0.8,
        density_level="busy",
    )
    skill = PedestrianDecisionSkill(config_path="missing-config.json")
    context = skill.build_context(profile, state, observation)
    print("Allowed actions:", ", ".join(sorted(skill.ALLOWED_ACTIONS)))
    print("Normalized context:", json.dumps(context, ensure_ascii=False, indent=2))
    print("Messages:", json.dumps(skill.build_messages(context), ensure_ascii=False, indent=2))
    print(
        "Validated model result:",
        skill.parse_decision(
            '{"action":"continue","target_id":null,"reason":"环境安全","confidence":0.9}'
        ),
    )


if __name__ == "__main__":
    main()
