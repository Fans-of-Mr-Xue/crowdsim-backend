"""Re-execute archived plans and compare them with a recorded SUMO trajectory."""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crowdsim.domain.crowdsim_models import BehaviorPlan
from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime


def load_plans(path: Path):
    by_time = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)["plan"]
        raw["route_edges"] = tuple(raw.get("route_edges", ()))
        raw["next_route_edges"] = tuple(raw.get("next_route_edges", ()))
        plan = BehaviorPlan(**raw)
        by_time.setdefault(round(plan.decided_at, 6), []).append(plan)
    return by_time


def config_route_files(config_path: Path):
    element = ET.parse(config_path).getroot().find("./input/route-files")
    return [(config_path.parent / value.strip()).resolve() for value in element.get("value", "").split(",") if value.strip()]


def contains_pedestrian_demand(path: Path) -> bool:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False
    return root.find("person") is not None or root.find("personFlow") is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--position-tolerance", type=float, default=1e-6)
    parser.add_argument("--speed-tolerance", type=float, default=1e-6)
    args = parser.parse_args()
    directory = args.run_directory.resolve()
    required = ["manifest.json", "demand.rou.xml", "profiles.jsonl", "decisions.jsonl", "lifecycle.jsonl", "trajectory.csv", "metrics.csv", "sumo.log", "summary.json"]
    missing = [name for name in required if not (directory / name).is_file()]
    if missing:
        report = {"status": "fail", "llm_called": False, "missing": missing}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    config_path = Path(manifest["config_path"])
    demand_path = directory / "demand.rou.xml"
    original_rows = list(csv.DictReader((directory / "trajectory.csv").open(encoding="utf-8")))
    expected = {(round(float(row["time"]), 6), row["person_id"]): row for row in original_rows}
    recorded_summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    max_time = max(max((float(row["time"]) for row in original_rows), default=0.0), float(recorded_summary.get("time_seconds", 0.0)))
    plans = load_plans(directory / "decisions.jsonl")
    routes = config_route_files(config_path)
    recorded_sources = {Path(path).resolve() for path in manifest.get("pedestrian_route_files", ())}
    non_pedestrian = [path for path in routes if path not in recorded_sources and not contains_pedestrian_demand(path)]
    override = [*non_pedestrian, demand_path]
    runtime = SimulationRuntime(config_path, pedestrian_route_files=[demand_path], extra_sumo_args=["--route-files", ",".join(str(path) for path in override)])
    max_position_error = 0.0
    max_speed_error = 0.0
    missing_samples = []
    try:
        runtime.initialize()
        runtime.use_llm = False
        runtime.start()
        while runtime.state == RuntimeState.RUNNING and runtime.time_seconds < max_time:
            for recorded in plans.get(round(runtime.time_seconds, 6), ()):
                if runtime.current and recorded.person_id in runtime.current.persons:
                    runtime.apply_plan(replace(recorded, snapshot_id=runtime.snapshot_id, source="replay"))
            result = runtime.tick()
            timestamp = round(result.time_seconds, 6)
            for person_id, motion in result.persons.items():
                row = expected.get((timestamp, person_id))
                if row is None:
                    missing_samples.append([timestamp, person_id, "unexpected"])
                    continue
                max_position_error = max(max_position_error, math.hypot(motion.x - float(row["x"]), motion.y - float(row["y"])))
                max_speed_error = max(max_speed_error, abs(motion.speed - float(row["speed"])))
            expected_ids = {person_id for time, person_id in expected if time == timestamp}
            missing_samples.extend([[timestamp, person_id, "missing"] for person_id in expected_ids - set(result.persons)])
    finally:
        runtime.close()
    report = {"status": "pass" if not missing_samples and max_position_error <= args.position_tolerance and max_speed_error <= args.speed_tolerance else "fail", "mode": "recorded_plan_reexecution", "llm_called": False, "trajectory_rows": len(original_rows), "recorded_decisions": sum(map(len, plans.values())), "max_position_error_m": max_position_error, "max_speed_error_mps": max_speed_error, "position_tolerance_m": args.position_tolerance, "speed_tolerance_mps": args.speed_tolerance, "population": runtime.population.diagnostics(), "sample_mismatches": missing_samples[:50], "replay_run_directory": str(runtime.recorder.directory if runtime.recorder else "")}
    (directory / "replay_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
