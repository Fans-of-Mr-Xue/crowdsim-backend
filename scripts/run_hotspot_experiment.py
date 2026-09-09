"""Verify localized crowd build-up and dispersal on the real Bund SUMO network."""

from __future__ import annotations

from collections import Counter
import argparse
import json
from pathlib import Path
import sys

from traci import constants as tc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crowdsim.environment.hotspot_catalog import HotspotCatalog
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.sumo_adapter import SumoAdapter


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sumo-config", type=Path, default=ROOT / "scenarios" / "shanghai_bund" / "bund.hotspot.sumocfg")
    parser.add_argument("--hotspot-config", type=Path, default=ROOT / "config" / "crowd_hotspots.json")
    parser.add_argument("--until", type=float, default=1200.0)
    parser.add_argument("--sample-seconds", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    network_path = args.sumo_config.parent / "bund.net.xml"
    network = ResearchNetwork(str(network_path))
    catalog = HotspotCatalog(network, args.hotspot_config)
    hotspot = catalog.hotspots["chen_yi_square"]
    measured_edges = set(hotspot["measurement_edges"])
    target_edge = hotspot["target_edge"]
    target = network.edges[target_edge]
    target_area = target.getLength() * sum(lane.getWidth() for lane in target.getLanes() if lane.allows("pedestrian"))
    adapter = SumoAdapter(args.sumo_config)
    departed = set()
    arrived = set()
    samples = []
    peak = None
    try:
        adapter.start()
        while adapter.time_seconds < args.until and adapter.min_expected_number > 0:
            step = adapter.advance_to(min(args.until, adapter.time_seconds + args.sample_seconds))
            departed.update(step.departed_person_ids)
            arrived.update(step.arrived_person_ids)
            core = [motion for motion in step.persons.values() if motion.edge_id == target_edge]
            core_walking = [motion for motion in core if motion.stage_type == tc.STAGE_WALKING]
            area = [motion for motion in step.persons.values() if motion.edge_id in measured_edges]
            outside_walking = [motion for motion in step.persons.values() if motion.edge_id not in measured_edges and motion.stage_type == tc.STAGE_WALKING]
            approach_walking = [motion for motion in area if motion.edge_id != target_edge and motion.stage_type == tc.STAGE_WALKING]
            sample = {
                "time_seconds": step.time_seconds,
                "active_total": len(step.persons),
                "hotspot_area_count": len(area),
                "core_count": len(core),
                "core_density_person_per_m2": len(core) / target_area,
                "core_low_speed_count": sum(motion.speed < 0.2 for motion in core),
                "core_walking_count": len(core_walking),
                "core_slow_walking_count": sum(motion.speed < 0.5 for motion in core_walking),
                "approach_walking_count": len(approach_walking),
                "approach_low_speed_walking_count": sum(motion.speed < 0.2 for motion in approach_walking),
                "outside_walking_count": len(outside_walking),
                "outside_walking_avg_speed_mps": sum(motion.speed for motion in outside_walking) / max(1, len(outside_walking)),
            }
            samples.append(sample)
            if peak is None or sample["core_count"] > peak["core_count"]:
                peak = sample
    finally:
        adapter.close()

    early = [item for item in samples if item["time_seconds"] <= 100]
    late = [item for item in samples if item["time_seconds"] >= 1100]
    baseline_core = max((item["core_count"] for item in early), default=0)
    late_core = max((item["core_count"] for item in late), default=0)
    congested_samples = [item for item in samples if item["core_density_person_per_m2"] >= 1.5]
    congested_approach_low_speed_max = max((item["approach_low_speed_walking_count"] for item in congested_samples), default=0)
    congested_core_slow_walking_max = max((item["core_slow_walking_count"] for item in congested_samples), default=0)
    checks = {
        "localized_accumulation_emerges": peak is not None and peak["core_count"] >= max(100, baseline_core * 5),
        "core_reaches_crowded_density": peak is not None and peak["core_density_person_per_m2"] >= 1.5,
        "walking_people_slow_without_planned_stop": congested_core_slow_walking_max > 0 or congested_approach_low_speed_max > 0,
        "outside_area_keeps_moving": peak is not None and peak["outside_walking_count"] > 0 and peak["outside_walking_avg_speed_mps"] >= 0.5,
        "hotspot_later_disperses": peak is not None and late_core <= peak["core_count"] * 0.35,
    }
    report = {
        "status": "pass" if all(checks.values()) else "fail",
        "scenario": "Bund background flow plus Chen Yi Square finite gathering",
        "sumo_model": "striping",
        "parameter_status": catalog.parameter_status,
        "checks": checks,
        "population": {"departed_by_end": len(departed), "arrived_by_end": len(arrived)},
        "baseline_core_max": baseline_core,
        "peak": peak,
        "late_core_max": late_core,
        "max_approach_low_speed_walking_count_while_core_crowded": congested_approach_low_speed_max,
        "max_core_slow_walking_count_while_core_crowded": congested_core_slow_walking_max,
        "samples": samples,
        "limitations": "Mechanism/emergence demonstration. Demand volume and dwell parameters are not calibrated to observed Bund data.",
    }
    output = ROOT / "runs" / "hotspot"
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "chen_yi_square_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = dict(report)
    summary.pop("samples")
    summary["sample_count"] = len(samples)
    summary["report"] = str(report_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
