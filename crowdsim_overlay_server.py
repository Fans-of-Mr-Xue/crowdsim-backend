"""Compatibility entry point backed exclusively by SUMO/TraCI."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
import xml.etree.ElementTree as ET

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.infrastructure.websocket_server import OverlayServer
from crowdsim.scenarios.generated_hotspot_demand import HotspotDemandSpec
from crowdsim.scenarios.generated_network_demand import NetworkDemandSpec
from pedestrian_decision_skill import PedestrianDecisionSkill


PROJECT_ROOT = Path(__file__).resolve().parent
SCENARIO_DIR = PROJECT_ROOT / "scenarios" / "shanghai_bund"


@dataclass(frozen=True)
class ServiceScenario:
    name: str
    config_path: Path
    pedestrian_route_files: tuple[Path, ...]
    demand_mode: str = "configurable"
    timeline_end_seconds: float | None = None
    location_id: str | None = None
    road_network_url: str | None = None
    hotspot_demand_spec: HotspotDemandSpec | None = None


SCENARIO_PRESETS = {
    "research": ServiceScenario(
        "research",
        (SCENARIO_DIR / "bund.research.sumocfg").resolve(),
        ((SCENARIO_DIR / "bund_ped.rou.xml").resolve(),),
    ),
    "hotspot": ServiceScenario(
        "hotspot",
        (SCENARIO_DIR / "bund.hotspot.sumocfg").resolve(),
        ((SCENARIO_DIR / "bund_hotspot.rou.xml").resolve(),),
        demand_mode="generated_hotspot",
        timeline_end_seconds=1800.0,
        location_id="memorial-tower",
        road_network_url="/static/crowd_sim/road_network.json",
        hotspot_demand_spec=HotspotDemandSpec(
            source_path=SCENARIO_DIR / "bund_ped.rou.xml",
            config_path=PROJECT_ROOT / "config/crowd_hotspots.json",
        ),
    ),
    "east-nanjing-road": ServiceScenario(
        "east-nanjing-road",
        (PROJECT_ROOT / "scenarios/east_nanjing_road/east_nanjing.sumocfg").resolve(),
        ((PROJECT_ROOT / "scenarios/east_nanjing_road/demo.rou.xml").resolve(),),
        demand_mode="generated_hotspot",
        timeline_end_seconds=1800.0,
        location_id="east-nanjing-road",
        road_network_url="/static/crowd_sim/east_nanjing_road_network.json",
        hotspot_demand_spec=HotspotDemandSpec(
            source_path=PROJECT_ROOT / "scenarios/east_nanjing_road/demo.rou.xml",
            config_path=PROJECT_ROOT / "scenarios/east_nanjing_road/crowd_hotspots.json",
            hotspot_id="chen_yi_square",
        ),
    ),
}


def default_scenario_dir() -> str:
    return str(PROJECT_ROOT / "scenarios" / "shanghai_bund")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SUMO-native CrowdSim backend")
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIO_PRESETS),
        default="research",
        help="initial preset; submitted requirements automatically select their matching built-in scenario",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="custom SUMO config; use together with --ped-routes unless it matches a built-in preset",
    )
    parser.add_argument(
        "--ped-routes",
        type=Path,
        nargs="+",
        help="pedestrian route files referenced by a custom --config",
    )
    parser.add_argument("--sumo-binary")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", choices=("rule", "llm"), default="rule")
    parser.add_argument("--deepseek-config", type=Path)
    return parser.parse_args(argv)


def resolve_service_scenario(args: argparse.Namespace) -> ServiceScenario:
    preset = SCENARIO_PRESETS[args.scenario]

    if args.config is None:
        if args.ped_routes:
            raise ValueError("--ped-routes requires --config")
        selection = preset
    else:
        config_path = args.config.resolve()
        route_files = tuple(path.resolve() for path in args.ped_routes or ())
        matched_preset = next(
            (item for item in SCENARIO_PRESETS.values() if config_path == item.config_path),
            None,
        )
        if not route_files and matched_preset is not None:
            route_files = matched_preset.pedestrian_route_files
        if not route_files:
            raise ValueError("custom --config requires at least one --ped-routes file")
        selection = ServiceScenario(
            matched_preset.name if matched_preset else "custom",
            config_path,
            route_files,
            demand_mode=matched_preset.demand_mode if matched_preset else "configurable",
            timeline_end_seconds=matched_preset.timeline_end_seconds if matched_preset else None,
            location_id=matched_preset.location_id if matched_preset else None,
            road_network_url=matched_preset.road_network_url if matched_preset else None,
            hotspot_demand_spec=matched_preset.hotspot_demand_spec if matched_preset else None,
        )

    _validate_service_scenario(selection)
    return selection


def _validate_service_scenario(selection: ServiceScenario) -> None:
    if not selection.config_path.is_file():
        raise FileNotFoundError(selection.config_path)
    for route_file in selection.pedestrian_route_files:
        if not route_file.is_file():
            raise FileNotFoundError(route_file)
    if selection.demand_mode == "generated_hotspot":
        if selection.hotspot_demand_spec is None:
            raise ValueError("generated hotspot scenario requires a hotspot demand specification")
        for path in (selection.hotspot_demand_spec.source_path, selection.hotspot_demand_spec.config_path):
            if not path.is_file():
                raise FileNotFoundError(path)
        selection.hotspot_demand_spec.configuration()

    root = ET.parse(selection.config_path).getroot()
    route_element = root.find("./input/route-files")
    configured = set()
    if route_element is not None:
        configured = {
            (selection.config_path.parent / value.strip()).resolve()
            for value in route_element.get("value", "").split(",")
            if value.strip()
        }
    missing = [path for path in selection.pedestrian_route_files if path not in configured]
    if missing:
        names = ", ".join(str(path) for path in missing)
        raise ValueError(f"pedestrian route files are not referenced by {selection.config_path}: {names}")
    if selection.timeline_end_seconds is not None:
        end_element = root.find("./time/end")
        configured_end = float(end_element.get("value", 0.0)) if end_element is not None else 0.0
        if configured_end < selection.timeline_end_seconds:
            raise ValueError(
                f"scenario ends at {configured_end}s before its required "
                f"{selection.timeline_end_seconds}s timeline"
            )


def build_runtime(args: argparse.Namespace, selection: ServiceScenario | None = None) -> SimulationRuntime:
    selection = selection or resolve_service_scenario(args)
    decision_engine = PedestrianDecisionSkill(config_path=args.deepseek_config) if args.mode == "llm" else None
    return SimulationRuntime(
        selection.config_path,
        pedestrian_route_files=selection.pedestrian_route_files,
        sumo_binary=args.sumo_binary,
        decision_engine=decision_engine,
        use_llm=args.mode == "llm",
        scenario_name=selection.name,
        demand_mode=selection.demand_mode,
        timeline_end_seconds=selection.timeline_end_seconds,
        location_id=selection.location_id,
        road_network_url=selection.road_network_url,
        network_demand_spec=NetworkDemandSpec() if selection.demand_mode == "generated_network" else None,
        hotspot_demand_spec=selection.hotspot_demand_spec,
    )


def build_requirement_runtime(args: argparse.Namespace, record: dict) -> SimulationRuntime:
    location_id = record["requirement"]["spatial_scope"]["location_id"]
    preset_name = {"memorial-tower": "hotspot", "east-nanjing-road": "east-nanjing-road"}.get(location_id)
    if preset_name is None:
        raise ValueError(f"no SUMO preset for location {location_id}")
    selection = SCENARIO_PRESETS[preset_name]
    _validate_service_scenario(selection)
    return build_runtime(args, selection)


def main() -> None:
    args = parse_args()
    try:
        selection = resolve_service_scenario(args)
    except (FileNotFoundError, ValueError, ET.ParseError) as exc:
        raise SystemExit(f"[CrowdSim] invalid scenario selection: {exc}") from exc
    print(
        f"[CrowdSim] scenario={selection.name} config={selection.config_path} "
        f"pedestrian_routes={','.join(str(path) for path in selection.pedestrian_route_files)} "
        f"demand_mode={selection.demand_mode} timeline_end={selection.timeline_end_seconds}"
    )
    runtime = build_runtime(args, selection)
    # Custom experiments retain their explicit network. The normal service can
    # bind either supported location when the client configures its requirement.
    factory = (lambda record: build_requirement_runtime(args, record)) if selection.name != "custom" else None
    asyncio.run(OverlayServer(runtime, args.host, args.port, runtime_factory=factory).start())


if __name__ == "__main__":
    main()
