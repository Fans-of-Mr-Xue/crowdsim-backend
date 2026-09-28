"""Verify localized crowd build-up and dispersal on the real Bund SUMO network."""

from __future__ import annotations

from collections import Counter
import argparse
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from traci import constants as tc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crowdsim.environment.hotspot_catalog import HotspotCatalog
from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
from crowdsim.infrastructure.hotspot_phase import HotspotPhaseTracker
from crowdsim.infrastructure.network_adapter import ResearchNetwork


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sumo-config", type=Path, default=ROOT / "scenarios" / "shanghai_bund" / "bund.hotspot.sumocfg")
    parser.add_argument("--hotspot-config", type=Path, default=ROOT / "config" / "crowd_hotspots.json")
    parser.add_argument("--hotspot-id", default="people_heroes_monument")
    parser.add_argument("--until", type=float, default=1800.0)
    parser.add_argument("--sample-seconds", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    network_path = args.sumo_config.parent / "bund.net.xml"
    network = ResearchNetwork(str(network_path))
    catalog = HotspotCatalog(network, args.hotspot_config)
    hotspot = catalog.hotspots[args.hotspot_id]
    measured_edges = set(hotspot["measurement_edges"])
    target_edges = set(hotspot["target_edges"])
    park_approach_edges = set(hotspot.get("park_access_edges", ())) | set(hotspot.get("entry_edges", ()))
    external_approach_edges = set(hotspot.get("external_approach_edges", ()))
    target_area = sum(
        network.edges[edge_id].getLength()
        * sum(lane.getWidth() for lane in network.edges[edge_id].getLanes() if lane.allows("pedestrian"))
        for edge_id in target_edges
    )
    phase_tracker = HotspotPhaseTracker(hotspot.get("phase_thresholds"))
    core_phase_tracker = HotspotPhaseTracker(hotspot.get("phase_thresholds"))
    congested_density = phase_tracker.thresholds.congested_core_density_person_per_m2
    visitor_prefix = f"hotspot.{args.hotspot_id}."
    planned_visitors = int(hotspot.get("visitor_count", 0))
    route_root = ET.parse(args.sumo_config).getroot()
    route_value = route_root.find("./input/route-files").get("value").split(",")[0].strip()
    route_path = (args.sumo_config.parent / route_value).resolve()
    runtime = SimulationRuntime(
        args.sumo_config,
        pedestrian_route_files=[route_path],
        scenario_name="hotspot",
        demand_mode="fixed",
        timeline_end_seconds=args.until,
    )
    samples = []
    peak = None
    try:
        runtime.initialize()
        runtime.start()
        while runtime.state == RuntimeState.RUNNING and runtime.time_seconds < args.until:
            sample_at = min(args.until, runtime.time_seconds + args.sample_seconds)
            while runtime.state == RuntimeState.RUNNING and runtime.time_seconds < sample_at:
                step = runtime.tick()
            core = [motion for motion in step.persons.values() if motion.edge_id in target_edges]
            core_dwelling = [
                motion for motion in core
                if runtime.population.states[motion.person_id].activity_state == "hotspot_dwelling"
            ]
            core_walking = [
                motion for motion in core
                if motion.stage_type == tc.STAGE_WALKING
                and runtime.population.states[motion.person_id].activity_state != "hotspot_dwelling"
            ]
            area = [motion for motion in step.persons.values() if motion.edge_id in measured_edges]
            outside_walking = [
                motion for motion in step.persons.values()
                if motion.edge_id not in measured_edges | park_approach_edges | external_approach_edges
                and motion.stage_type == tc.STAGE_WALKING
            ]
            approach_walking = [
                motion for motion in step.persons.values()
                if motion.edge_id in park_approach_edges and motion.stage_type == tc.STAGE_WALKING
            ]
            external_approach_walking = [
                motion for motion in step.persons.values()
                if motion.edge_id in external_approach_edges and motion.stage_type == tc.STAGE_WALKING
            ]
            active_visitors = [
                motion for person_id, motion in step.persons.items()
                if person_id.startswith(visitor_prefix)
            ]
            completed_visitors = sum(
                person_id.startswith(visitor_prefix)
                for person_id in runtime.population.ledger.arrived_ids
            )
            remaining_visitors = max(0, planned_visitors - completed_visitors)
            blocked_approach = sum(motion.speed < 0.2 for motion in approach_walking)
            blocked_external_approach = sum(
                motion.speed < 0.2 for motion in external_approach_walking
            )
            sample = {
                "time_seconds": step.time_seconds,
                "active_total": len(step.persons),
                "hotspot_area_count": len(area),
                "core_count": len(core),
                "core_density_person_per_m2": len(core) / target_area,
                "core_low_speed_count": sum(motion.speed < 0.2 for motion in core),
                "core_dwelling_count": len(core_dwelling),
                "core_walking_count": len(core_walking),
                "core_slow_walking_count": sum(motion.speed < 0.5 for motion in core_walking),
                "approach_walking_count": len(approach_walking),
                "approach_low_speed_walking_count": blocked_approach,
                "external_approach_walking_count": len(external_approach_walking),
                "external_approach_low_speed_walking_count": blocked_external_approach,
                "active_hotspot_visitor_count": len(active_visitors),
                "completed_hotspot_visitor_count": completed_visitors,
                "remaining_hotspot_visitor_count": remaining_visitors,
                "outside_walking_count": len(outside_walking),
                "outside_walking_avg_speed_mps": sum(motion.speed for motion in outside_walking) / max(1, len(outside_walking)),
            }
            sample["process_state"] = phase_tracker.update(
                step.time_seconds,
                sample["hotspot_area_count"],
                sample["core_density_person_per_m2"],
                remaining_visitor_count=remaining_visitors,
                blocked_person_count=blocked_approach + blocked_external_approach,
            )
            sample["core_process_state"] = core_phase_tracker.update(
                step.time_seconds,
                sample["hotspot_area_count"],
                sample["core_density_person_per_m2"],
            )
            samples.append(sample)
            if peak is None or sample["core_count"] > peak["core_count"]:
                peak = sample
    finally:
        runtime.close()

    departed = runtime.population.ledger.departed_ids
    arrived = runtime.population.ledger.arrived_ids

    early = [item for item in samples if item["time_seconds"] <= 100]
    late = [item for item in samples if item["time_seconds"] >= args.until - 100]
    baseline_core = max((item["core_count"] for item in early), default=0)
    late_core = max((item["core_count"] for item in late), default=0)
    congested_samples = [item for item in samples if item["core_density_person_per_m2"] >= congested_density]
    congested_approach_low_speed_max = max((item["approach_low_speed_walking_count"] for item in congested_samples), default=0)
    congested_external_low_speed_max = max((item["external_approach_low_speed_walking_count"] for item in congested_samples), default=0)
    congested_core_slow_walking_max = max((item["core_slow_walking_count"] for item in congested_samples), default=0)
    observed_phases = [item["phase"] for item in phase_tracker.transitions]
    observed_core_phases = [item["phase"] for item in core_phase_tracker.transitions]
    late_blocked = max((
        item["approach_low_speed_walking_count"] + item["external_approach_low_speed_walking_count"]
        for item in late
    ), default=0)
    checks = {
        "localized_accumulation_emerges": peak is not None and peak["core_count"] >= max(100, baseline_core * 5),
        "core_reaches_crowded_density": peak is not None and peak["core_density_person_per_m2"] >= congested_density,
        "walking_people_slow_without_planned_stop": (
            congested_core_slow_walking_max > 0
            or congested_approach_low_speed_max > 0
            or congested_external_low_speed_max > 0
        ),
        "visitors_start_outside_park": any(item["external_approach_walking_count"] > 0 for item in samples),
        "outside_area_keeps_moving": peak is not None and peak["outside_walking_count"] > 0 and peak["outside_walking_avg_speed_mps"] >= 0.5,
        "hotspot_later_disperses": peak is not None and late_core <= peak["core_count"] * 0.35,
        "core_ordered_process_states_observed": observed_core_phases == ["normal", "building", "congested", "dispersing", "cleared"],
        "residual_congestion_is_reported": observed_phases == ["normal", "building", "congested", "dispersing", "residual_congestion"],
        "overall_state_does_not_false_clear": late_blocked <= phase_tracker.thresholds.cleared_blocked_person_count or phase_tracker.phase != "cleared",
    }
    report = {
        "status": "pass" if all(checks.values()) else "fail",
        "scenario": (
            "Bund "
            + ("background flow plus " if int(hotspot.get("background_count", 0)) else "")
            + f"{hotspot.get('name', args.hotspot_id)} finite gathering"
        ),
        "hotspot_id": args.hotspot_id,
        "sumo_model": "striping",
        "parameter_status": catalog.parameter_status,
        "checks": checks,
        "population": {"departed_by_end": len(departed), "arrived_by_end": len(arrived)},
        "baseline_core_max": baseline_core,
        "peak": peak,
        "late_core_max": late_core,
        "max_approach_low_speed_walking_count_while_core_crowded": congested_approach_low_speed_max,
        "max_external_approach_low_speed_walking_count_while_core_crowded": congested_external_low_speed_max,
        "max_core_slow_walking_count_while_core_crowded": congested_core_slow_walking_max,
        "process_transitions": phase_tracker.transitions,
        "core_process_transitions": core_phase_tracker.transitions,
        "late_approach_low_speed_max": late_blocked,
        "samples": samples,
        "limitations": "Mechanism/emergence demonstration. Demand volume and dwell parameters are not calibrated to observed Bund data.",
    }
    output = ROOT / "runs" / "hotspot"
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / f"{args.hotspot_id}_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = dict(report)
    summary.pop("samples")
    summary["sample_count"] = len(samples)
    summary["report"] = str(report_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
