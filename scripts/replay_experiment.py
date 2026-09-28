"""Stream archived plans/trajectories and compare real SUMO re-execution."""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
from crowdsim.infrastructure.decision_log_reader import DecisionLogError, DecisionLogReader


def iter_trajectory_batches(path):
    previous = None
    batch = {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not {"time", "person_id", "x", "y", "speed"}.issubset(reader.fieldnames or ()):
            raise DecisionLogError("trajectory CSV is missing required columns")
        for number, row in enumerate(reader, 2):
            try:
                timestamp = round(float(row["time"]), 6)
                values = [float(row[name]) for name in ("x", "y", "speed")]
            except (TypeError, ValueError) as exc:
                raise DecisionLogError(f"invalid trajectory row {number}") from exc
            if not all(math.isfinite(value) for value in [timestamp, *values]) or not row["person_id"]:
                raise DecisionLogError(f"invalid trajectory values at row {number}")
            if previous is not None and timestamp < previous:
                raise DecisionLogError("trajectory times are out of order")
            if batch and timestamp != previous:
                yield previous, batch
                batch = {}
            if row["person_id"] in batch:
                raise DecisionLogError(f"duplicate trajectory person at time {timestamp}")
            previous = timestamp
            batch[row["person_id"]] = row
        if batch:
            yield previous, batch


class BatchCursor:
    def __init__(self, iterator):
        self.iterator = iter(iterator)
        self.pending = next(self.iterator, None)

    def take(self, timestamp, default):
        if self.pending is not None and self.pending[0] < timestamp:
            raise DecisionLogError(f"unconsumed input at {self.pending[0]} before simulation time {timestamp}")
        if self.pending is None or self.pending[0] != timestamp:
            return default
        _, result = self.pending
        self.pending = next(self.iterator, None)
        return result

    def close(self):
        close = getattr(self.iterator, "close", None)
        if close:
            close()


def config_route_files(config_path):
    element = ET.parse(config_path).getroot().find("./input/route-files")
    return [(config_path.parent / value.strip()).resolve() for value in element.get("value", "").split(",") if value.strip()]


def contains_pedestrian_demand(path):
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False
    return root.find("person") is not None or root.find("personFlow") is not None


def replay_tick(runtime, plans):
    # Replay-only: preserve preparation/application order, without new rules/LLM.
    runtime._prepare_boundary()
    for plan in plans:
        if plan.person_id not in runtime.current.persons:
            raise DecisionLogError(f"recorded plan refers to inactive person {plan.person_id}")
    runtime.decision_scheduler._mark_scheduled(
        [plan.person_id for plan in plans], runtime.population.states, runtime.observations)
    return runtime._step_after_plans([
        replace(plan, snapshot_id=runtime.snapshot_id, source="replay") for plan in plans])


def replay_experiment(directory, *, position_tolerance=1e-6, speed_tolerance=1e-6):
    directory = Path(directory).resolve()
    report = {
        "report_version": 2, "status": "fail", "mode": "recorded_plan_reexecution",
        "llm_called": False, "fresh_decisions_disabled": True,
        "source_run_directory": str(directory), "input_format": None, "input_format_version": None,
        "position_tolerance_m": position_tolerance, "speed_tolerance_mps": speed_tolerance,
        "trajectory_rows": 0, "recorded_decisions": 0, "mismatch_count": 0, "sample_mismatches": [],
    }
    runtime = reader = plans = trajectory = None
    max_position_error = max_speed_error = 0.0

    def mismatch(value):
        report["mismatch_count"] += 1
        if len(report["sample_mismatches"]) < 50:
            report["sample_mismatches"].append(value)

    try:
        if any(not math.isfinite(value) or value < 0 for value in (position_tolerance, speed_tolerance)):
            raise DecisionLogError("replay tolerances must be finite and non-negative")
        required = ["manifest.json", "demand.rou.xml", "profiles.jsonl", "decisions.jsonl",
                    "lifecycle.jsonl", "trajectory.csv", "metrics.csv", "sumo.log", "summary.json"]
        missing = [name for name in required if not (directory / name).is_file()]
        if missing:
            report["missing"] = missing
            raise DecisionLogError(f"missing replay inputs: {missing}")
        reader = DecisionLogReader(directory)
        manifest = reader.manifest
        report.update(input_format=reader.format, input_format_version=reader.format_version,
                      source_run_id=manifest["run_id"],
                      source_manifest_sha256=hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest())
        inputs = ["decisions.jsonl", "trajectory.csv", "demand.rou.xml", "summary.json", "profiles.jsonl"]
        if (directory / "commands.jsonl").is_file():
            inputs.append("commands.jsonl")
        if reader.table_manifest is not None:
            report["source_decision_manifest_sha256"] = hashlib.sha256((directory / "decision_manifest.json").read_bytes()).hexdigest()
            report["validated_decision_tables"] = ["decisions", "plans", "routes"]
            inputs += ["decision_plans.jsonl", "decision_routes.jsonl"]
        report["input_file_stats"] = {
            name: {"bytes": (directory / name).stat().st_size, "mtime_ns": (directory / name).stat().st_mtime_ns}
            for name in inputs}
        config_path = Path(manifest["config_path"])
        if hashlib.sha256(config_path.read_bytes()).hexdigest() != manifest.get("config_sha256"):
            raise DecisionLogError("SUMO config has changed since recording; restore the recorded config before replay")
        demand_path = directory / "demand.rou.xml"
        if hashlib.sha256(demand_path.read_bytes()).hexdigest() != manifest.get("demand_sha256"):
            raise DecisionLogError("archived demand checksum mismatch")
        commands = directory / "commands.jsonl"
        if commands.is_file():
            with commands.open("rb") as handle:
                if any(line.strip() for line in handle):
                    raise DecisionLogError("recorded external commands are not supported by basic plan-only replay")
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        max_time = float(summary.get("time_seconds", 0.0))
        if not math.isfinite(max_time) or max_time <= 0:
            raise DecisionLogError("recorded summary has no positive, finite duration")
        plans = BatchCursor(reader.iter_plan_batches())
        trajectory = BatchCursor(iter_trajectory_batches(directory / "trajectory.csv"))
        if trajectory.pending is None:
            raise DecisionLogError("recorded trajectory contains no samples")
        routes = config_route_files(config_path)
        recorded_sources = {Path(path).resolve() for path in manifest.get("pedestrian_route_files", ())}
        non_pedestrian = [path for path in routes if path not in recorded_sources and not contains_pedestrian_demand(path)]
        override = [*non_pedestrian, demand_path]
        runtime = SimulationRuntime(config_path, pedestrian_route_files=[demand_path],
                                   extra_sumo_args=["--route-files", ",".join(str(path) for path in override)])
        runtime.population.profile_sampler.seed = manifest.get("profile_seed", runtime.population.profile_sampler.seed)
        runtime.initialize()
        recorded_version = manifest.get("engine", {}).get("sumo_version")
        report["sumo_version"] = runtime.adapter.diagnostics.get("sumo_version")
        if recorded_version and report["sumo_version"] != recorded_version:
            raise DecisionLogError("SUMO version differs from recorded run")
        runtime.use_llm = False
        runtime.recorder.update_manifest(replay_source_run_id=manifest["run_id"], replay_source_format=reader.format)
        runtime.start()
        while runtime.state == RuntimeState.RUNNING and runtime.time_seconds < max_time:
            batch = plans.take(round(runtime.time_seconds, 6), [])
            report["recorded_decisions"] += len(batch)
            result = replay_tick(runtime, batch)
            timestamp = round(result.time_seconds, 6)
            expected = trajectory.take(timestamp, {})
            report["trajectory_rows"] += len(expected)
            for person_id, motion in result.persons.items():
                row = expected.get(person_id)
                if row is None:
                    mismatch([timestamp, person_id, "unexpected"])
                    continue
                max_position_error = max(max_position_error, math.hypot(motion.x - float(row["x"]), motion.y - float(row["y"])))
                max_speed_error = max(max_speed_error, abs(motion.speed - float(row["speed"])))
            for person_id in expected.keys() - result.persons.keys():
                mismatch([timestamp, person_id, "missing"])
        if plans.pending is not None or trajectory.pending is not None:
            raise DecisionLogError("unconsumed plans/trajectory remain after replay")
        if not math.isclose(runtime.time_seconds, max_time, abs_tol=1e-6):
            raise DecisionLogError("replay ended at a different simulation time")
        population = runtime.population.diagnostics()
        recorded_population = summary.get("population", {})
        population_match = all(population.get(key) == recorded_population.get(key) for key in
                               ("planned", "departed", "active", "arrived", "explicitly_removed", "conservation_error", "unknown_disappearances"))
        report.update(population=population, population_match=population_match)
        if (report["trajectory_rows"] > 0 and not report["mismatch_count"] and population_match
                and max_position_error <= position_tolerance and max_speed_error <= speed_tolerance):
            report["status"] = "pass"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if runtime is not None:
            runtime.last_error = report["error"]
            runtime.state = RuntimeState.ERROR
    finally:
        for cursor in (plans, trajectory):
            if cursor is not None:
                cursor.close()
        if reader is not None:
            reader.close()
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:
                report.update(status="fail", cleanup_error=f"{type(exc).__name__}: {exc}")
            if runtime.recorder is not None:
                report["replay_run_directory"] = str(runtime.recorder.directory)
                report["replay_run_id"] = runtime.run_id
        report.update(max_position_error_m=max_position_error, max_speed_error_mps=max_speed_error)
    report["code_commit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--position-tolerance", type=float, default=1e-6)
    parser.add_argument("--speed-tolerance", type=float, default=1e-6)
    parser.add_argument("--output", type=Path, help="report path; defaults to the NEW replay run directory, not the source run")
    args = parser.parse_args()
    report = replay_experiment(args.run_directory, position_tolerance=args.position_tolerance, speed_tolerance=args.speed_tolerance)
    output = args.output or (Path(report["replay_run_directory"]) / "replay_report.json" if report.get("replay_run_directory") else None)
    if output is not None:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["report_path"] = str(output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
