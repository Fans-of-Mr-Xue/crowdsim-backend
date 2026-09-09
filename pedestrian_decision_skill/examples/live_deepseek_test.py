"""Run exactly one real DeepSeek request for a controlled smoke test."""

import argparse
import asyncio
import json
import sys
import time

from pedestrian_decision_skill import (
    DeepSeekClient,
    DeepSeekClientError,
    DeepSeekConfigError,
    DecisionValidationError,
    PedestrianDecisionSkill,
    load_deepseek_config,
)


TEST_CONTEXT = {
    "agent_id": "live-test-agent",
    "profile": {
        "nationality": "CN",
        "language": "zh",
    },
    "current_state": {
        "status": "walking",
        "speed": 1.1,
        "stress": 0.35,
        "fatigue": 0.2,
        "flood_impact": 0.1,
        "event_impact": 0.25,
    },
    "surrounding_crowd": {
        "nearby_people": 12,
        "local_density": 1.8,
        "density_level": "busy",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one paid DeepSeek request and validate its decision JSON.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="confirm that one real API request may be sent",
    )
    return parser.parse_args()


async def run_live_test() -> int:
    try:
        config = load_deepseek_config()
    except DeepSeekConfigError as exc:
        print(f"配置检查失败：{exc}", file=sys.stderr)
        return 2

    print("即将发送一次 DeepSeek API 请求。")
    print(f"Base URL: {config['base_url']}")
    print(f"Model: {config['model']}")
    print(f"Timeout: {config['timeout_seconds']} seconds")
    print("API key: configured (value hidden)")
    print("Test context:")
    print(json.dumps(TEST_CONTEXT, ensure_ascii=False, indent=2))

    client = DeepSeekClient(config)
    skill = PedestrianDecisionSkill(client=client)
    messages = skill.build_messages(TEST_CONTEXT)
    started_at = time.perf_counter()

    try:
        raw_output = await client.complete(messages)
        result = skill.parse_decision(raw_output)
    except (DeepSeekClientError, DecisionValidationError) as exc:
        elapsed = time.perf_counter() - started_at
        print(f"实时 API 测试失败（{elapsed:.2f}s）：{exc}", file=sys.stderr)
        return 1

    elapsed = time.perf_counter() - started_at
    print(f"实时 API 测试成功（{elapsed:.2f}s）。")
    print("Validated decision:")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    args = parse_args()
    if not args.live:
        print(
            "未发送请求。确认 config.json 已填写后，请增加 --live 参数。",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(run_live_test())


if __name__ == "__main__":
    raise SystemExit(main())
