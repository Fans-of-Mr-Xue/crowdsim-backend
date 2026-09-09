"""Run one deterministic SUMO rule/LLM experiment."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sumo-config", type=Path, default=ROOT / "scenarios" / "shanghai_bund" / "bund.hotspot.sumocfg")
    parser.add_argument("--ped-routes", type=Path, default=ROOT / "scenarios" / "shanghai_bund" / "bund_hotspot.rou.xml")
    parser.add_argument("--count", type=int)
    parser.add_argument("--steps", type=int, default=120)
    parser.add_argument("--until-finished", action="store_true", help="run until SUMO reports no expected entities")
    parser.add_argument("--max-steps", type=int, default=10000, help="safety limit used with --until-finished")
    parser.add_argument("--mode", choices=("rule", "llm"), default="rule")
    parser.add_argument("--pair-id")
    parser.add_argument("--variant", default="baseline")
    return parser.parse_args()


async def run(args):
    runtime = SimulationRuntime(args.sumo_config, pedestrian_route_files=[args.ped_routes])
    if args.count is not None:
        runtime.configure_demand(args.count)
    try:
        runtime.initialize()
        runtime.use_llm = args.mode == "llm"
        runtime.recorder.update_manifest(mode=args.mode, model_id=runtime.decision_engine.model if runtime.use_llm else None, pair_id=args.pair_id, variant=args.variant)
        runtime.start()
        step_limit = args.max_steps if args.until_finished else args.steps
        for _ in range(step_limit):
            if runtime.state != RuntimeState.RUNNING:
                break
            await runtime.tick_async()
        if args.until_finished and runtime.state == RuntimeState.RUNNING:
            raise RuntimeError(f"simulation did not finish within {args.max_steps} steps")
    finally:
        runtime.close()
    return {"run_id": runtime.run_id, "run_directory": str(runtime.recorder.directory), "diagnostics": runtime.diagnostics()}


def main():
    result = asyncio.run(run(parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
