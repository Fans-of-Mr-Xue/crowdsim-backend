"""Run isolated, real-SUMO stage-0 measurements without a server or model call."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
from crowdsim.domain.during_crowding import summarize_crowding
from crowdsim.domain.during_preset import load_preset_file
from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.scenarios.generated_hotspot_demand import HotspotDemandSpec


def load_preset(path):
    return load_preset_file(path)


def sample_runtime(runtime, preset):
    people = [motion for motion in runtime.current.persons.values()
              if motion.edge_id in preset["phenomenon"]["scope_edges"]]
    counts = Counter(runtime.visual_states[item.person_id]["visual_state"] for item in people)
    population = runtime.population.diagnostics()
    hotspot = runtime.latest_metrics["hotspots"][preset["hotspot_id"]]
    return {
        "time_seconds": runtime.time_seconds, "core_count": len(people),
        "crowded_count": counts["crowded"], "high_risk_count": counts["high_risk"],
        "planned_stop_count": counts["planned_stop"],
        "unknown_count": sum(not runtime.visual_states[item.person_id]["density_valid"] for item in people),
        "core_density_person_per_m2": hotspot["density_person_per_m2"],
        "active_count": population["active"], "arrived_count": population["arrived"],
        "completion_rate": population["arrived"] / population["planned"] if population["planned"] else 0.0,
        "global_mean_speed_mps": runtime.latest_metrics["pedestrian_avg_speed_mps"],
        "blocked_low_speed_count": runtime.latest_metrics["queue"]["blocked_low_speed"],
        "control_held_count": len(set().union(*runtime._inflow_holds.values())) if runtime._inflow_holds else 0,
    }


def run_one(preset, count, seed, duration, variant, output_dir):
    payload = deepcopy(preset["requirement"])
    payload["population"]["total"] = count
    requirement = RequirementRepository(output_dir / "requirements").create(RequirementSpec.parse(payload))
    runtime = SimulationRuntime(
        ROOT / preset["sumo_config"],
        pedestrian_route_files=[ROOT / "scenarios/shanghai_bund/bund_hotspot.rou.xml"],
        demand_mode="generated_hotspot", scenario_name=preset["scenario"],
        location_id=preset["location_id"], timeline_end_seconds=duration,
        hotspot_demand_spec=HotspotDemandSpec(ROOT / preset["source_demand"], ROOT / preset["hotspot_config"],
                                            hotspot_id=preset["hotspot_id"], seed=seed),
        requirement_record=requirement, random_seed=seed,
        extra_sumo_args=["--seed", str(seed)],
    )
    started = time.perf_counter()
    samples = []
    action_result = None
    smoke = preset["control_scope"]["smoke_check"]
    try:
        runtime.configure_demand(count)
        runtime.initialize()
        if runtime.population.diagnostics()["planned"] != count:
            raise RuntimeError("generated population does not match requested count")
        runtime.recorder.update_manifest(stage="during_stage0", preset_id=preset["preset_id"],
                                         preset_version=preset["version"], variant=variant,
                                         resource_sha256=preset["resource_sha256"])
        if abs(runtime.step_length - preset["experiment"]["step_seconds"]) > 1e-9:
            raise ValueError("preset and runtime step lengths differ")
        samples.append(sample_runtime(runtime, preset))
        runtime.start()
        next_progress = 120
        while runtime.state == RuntimeState.RUNNING and runtime.time_seconds < duration:
            if variant == "metered" and action_result is None and runtime.time_seconds >= smoke["apply_at_seconds"]:
                action = {
                    "schemaVersion": "1.0", "actionId": f"stage0-meter-{seed}", "actionType": "set_inflow_rate",
                    "target": {"entryId": smoke["entry_id"]},
                    "parameters": {"rate": smoke["rate"], "durationSeconds": smoke["duration_seconds"]},
                }
                action_result = runtime.apply_control_action(action)
                runtime.recorder.record_command({"action": "apply_action", "controlAction": action}, action_result)
            runtime.tick()
            samples.append(sample_runtime(runtime, preset))
            if runtime.time_seconds >= next_progress:
                print(json.dumps({"count": count, "seed": seed, "variant": variant,
                                  "sim_seconds": runtime.time_seconds, "wall_seconds": round(time.perf_counter()-started, 1),
                                  "core_count": samples[-1]["core_count"], "crowded": samples[-1]["crowded_count"]+samples[-1]["high_risk_count"]}), flush=True)
                next_progress += 120
        population = runtime.population.diagnostics()
        if runtime.last_error or population["conservation_error"] != 0:
            raise RuntimeError(f"invalid run: {runtime.last_error or population}")
        all_arrived = population["arrived"] == count and population["active"] == 0
        if runtime.time_seconds < duration - 1e-9 and not all_arrived:
            raise RuntimeError("run ended before the configured observation horizon")
        report = {
            "preset_id": preset["preset_id"], "preset_version": preset["version"],
            "run_id": runtime.run_id, "requirement_fingerprint": requirement["fingerprint"],
            "count": count, "seed": seed, "variant": variant, "duration_seconds": duration,
            "observed_until_seconds": runtime.time_seconds,
            "end_reason": "all_arrived" if all_arrived else "observation_horizon",
            "sumo_version": runtime.adapter.diagnostics["sumo_version"],
            "wall_seconds": round(time.perf_counter()-started, 3),
            "population": population, "phenomenon": summarize_crowding(samples, preset["phenomenon"]),
            "peak_core_count": max(row["core_count"] for row in samples),
            "peak_core_density": max(row["core_density_person_per_m2"] for row in samples),
            "peak_planned_stop_count": max(row["planned_stop_count"] for row in samples),
            "peak_control_held_count": max(row["control_held_count"] for row in samples),
            "completion_rate": samples[-1]["completion_rate"],
            "t95_from_start_seconds": next((row["time_seconds"] for row in samples if row["completion_rate"] >= .95), None),
            "control_smoke_result": action_result,
            "raw_run_directory": str(runtime.recorder.directory),
            "final_sample": samples[-1],
        }
    finally:
        runtime.close()
    if runtime.close_errors:
        raise RuntimeError(f"runtime cleanup failed: {runtime.close_errors}")
    stem = f"{count}-{seed}-{variant}"
    with (output_dir / f"{stem}.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(samples[0]))
        writer.writeheader()
        writer.writerows(samples)
    report["samples_csv"] = str(output_dir / f"{stem}.csv")
    (output_dir / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"completed": stem, "occurred": report["phenomenon"]["occurred"],
                      "first_confirmation": report["phenomenon"]["first_confirmed_at_seconds"],
                      "peak_crowded": report["phenomenon"]["peak_crowded_count"],
                      "completion_rate": report["completion_rate"], "wall_seconds": report["wall_seconds"]}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", type=Path, default=ROOT / "config/during_presets/memorial_tower_v1.json")
    parser.add_argument("--counts", type=int, nargs="+")
    parser.add_argument("--seeds", type=int, nargs="+")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--variant", choices=["baseline", "metered"], default="baseline")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    preset = load_preset(args.preset)
    duration = args.duration if args.duration is not None else preset["experiment"]["duration_seconds"]
    if not math.isfinite(duration) or duration <= 0 or duration > 1800:
        parser.error("stage-0 duration must be in (0, 1800]")
    output = args.output_dir or ROOT / "runs" / "during_stage0" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    for count in args.counts or [preset["requirement"]["population"]["total"]]:
        for seed in args.seeds or preset["experiment"]["seed_set"][:1]:
            reports.append(run_one(preset, count, seed, duration, args.variant, output))
            (output / "summary.json").write_text(json.dumps({"resource_sha256": preset["resource_sha256"],
                "preset": preset, "runs": reports}, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
