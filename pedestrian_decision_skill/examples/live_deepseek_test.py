"""Run exactly one real DeepSeek request and obtain a ``BehaviorPlan``."""

import argparse
import asyncio
from dataclasses import asdict
import json
import sys
import time

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot, Observation
from pedestrian_decision_skill import DeepSeekClient, DeepSeekConfigError, PedestrianDecisionSkill, load_deepseek_config


def test_values():
    profile = AgentProfile("live-test-agent", nationality="CN", native_language="zh")
    state = AgentState("live-test-agent", stress=0.35, fatigue=0.2, perceived_risk=0.25)
    motion = MotionSnapshot("live-test-agent", 1.0, 0, 0, 121.49, 31.24, 1.1, "edge", "edge_0", 0, 90, 0, 2)
    observation = Observation(
        "live-test-agent",
        "live-test:1",
        1.0,
        motion,
        neighbour_ids=tuple(f"nearby-{index}" for index in range(12)),
        local_people_count=13,
        objective_density_per_m2=1.8,
        perceived_crowding=0.55,
        perceived_risk=0.25,
        density_level="crowded",
        flood_impact=0.1,
        event_impact=0.25,
    )
    return profile, state, observation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send one paid DeepSeek request and validate its BehaviorPlan.")
    parser.add_argument("--live", action="store_true", help="confirm that one real API request may be sent")
    return parser.parse_args()


async def run_live_test() -> int:
    try:
        config = load_deepseek_config()
    except DeepSeekConfigError as exc:
        print(f"配置检查失败：{exc}", file=sys.stderr)
        return 2

    skill = PedestrianDecisionSkill(client=DeepSeekClient(config))
    values = test_values()
    print("即将发送一次 DeepSeek API 请求。")
    print(f"Base URL: {config['base_url']}")
    print(f"Model: {config['model']}")
    print(f"Timeout: {config['timeout_seconds']} seconds")
    print("API key: configured (value hidden)")
    print("Test context:")
    print(json.dumps(skill.build_context(*values), ensure_ascii=False, indent=2))
    started_at = time.perf_counter()
    try:
        plan = await skill.decide(*values)
    except Exception as exc:
        elapsed = time.perf_counter() - started_at
        print(f"实时 API 测试失败（{elapsed:.2f}s）：{exc}", file=sys.stderr)
        return 1
    elapsed = time.perf_counter() - started_at
    if plan.source != "llm":
        print(f"实时 API 测试失败（{elapsed:.2f}s）：{skill.last_error or 'used rule fallback'}", file=sys.stderr)
        return 1
    print(f"实时 API 测试成功（{elapsed:.2f}s）。")
    print("Validated BehaviorPlan:")
    print(json.dumps(asdict(plan), ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    args = parse_args()
    if not args.live:
        print("未发送请求。确认 config.json 已填写后，请增加 --live 参数。", file=sys.stderr)
        return 2
    return asyncio.run(run_live_test())


if __name__ == "__main__":
    raise SystemExit(main())
