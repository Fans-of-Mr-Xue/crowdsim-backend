"""Compatibility entry point backed exclusively by SUMO/TraCI."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.infrastructure.websocket_server import OverlayServer
from pedestrian_decision_skill import PedestrianDecisionSkill


PROJECT_ROOT = Path(__file__).resolve().parent


def default_scenario_dir() -> str:
    return str(PROJECT_ROOT / "scenarios" / "shanghai_bund")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SUMO-native CrowdSim backend")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "scenarios" / "shanghai_bund" / "bund.research.sumocfg"))
    parser.add_argument("--sumo-binary")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", choices=("rule", "llm"), default="rule")
    parser.add_argument("--deepseek-config", type=Path)
    return parser.parse_args()


def build_runtime(args: argparse.Namespace) -> SimulationRuntime:
    config_path = Path(args.config).resolve()
    pedestrian_routes = [config_path.parent / "bund_ped.rou.xml"] if config_path.name.startswith("bund.") else []
    decision_engine = PedestrianDecisionSkill(config_path=args.deepseek_config) if args.mode == "llm" else None
    return SimulationRuntime(config_path, pedestrian_route_files=pedestrian_routes, sumo_binary=args.sumo_binary, decision_engine=decision_engine, use_llm=args.mode == "llm")


def main() -> None:
    args = parse_args()
    runtime = build_runtime(args)
    asyncio.run(OverlayServer(runtime, args.host, args.port).start())


if __name__ == "__main__":
    main()
