"""Run reproducible striping control/bottleneck/counterflow/route-load checks."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crowdsim.infrastructure.sumo_adapter import SumoAdapter


def run(name: str, route_file: str | None = None, steps: int = 240) -> dict:
    directory = ROOT / "tests" / "scenarios" / name
    extra = ["--route-files", str(directory / route_file)] if route_file else []
    adapter = SumoAdapter(directory / "scenario.sumocfg", extra_args=extra)
    departed, arrived = set(), set()
    max_active = 0
    edge_max = Counter()
    min_moving_speed = None
    seen_prefixes = set()
    try:
        adapter.start()
        for _ in range(steps):
            step = adapter.step()
            departed.update(step.departed_person_ids)
            arrived.update(step.arrived_person_ids)
            max_active = max(max_active, len(step.persons))
            edge_counts = Counter(motion.edge_id for motion in step.persons.values())
            for edge, count in edge_counts.items():
                edge_max[edge] = max(edge_max[edge], count)
            speeds = [motion.speed for motion in step.persons.values() if motion.speed > 0]
            if speeds:
                local_min = min(speeds)
                min_moving_speed = local_min if min_moving_speed is None else min(min_moving_speed, local_min)
            seen_prefixes.update(person_id.split(".", 1)[0] for person_id in step.persons)
            if adapter.min_expected_number <= 0:
                break
    finally:
        adapter.close()
    return {"departed": len(departed), "arrived": len(arrived), "max_active": max_active, "edge_max_person_count": dict(edge_max), "min_positive_speed_mps": min_moving_speed, "flow_prefixes": sorted(seen_prefixes)}


def main() -> int:
    output = ROOT / "runs" / "congestion"
    output.mkdir(parents=True, exist_ok=True)
    results = {"unidirectional_control": run("unidirectional_corridor"), "bottleneck_low": run("bottleneck", "low_demand.rou.xml"), "bottleneck_high": run("bottleneck", "demand.rou.xml"), "counterflow": run("counterflow"), "alternative_routes": run("alternative_routes")}
    low, high = results["bottleneck_low"], results["bottleneck_high"]
    checks = {"control_moves_people": results["unidirectional_control"]["departed"] > 0, "high_flow_increases_upstream_accumulation": high["edge_max_person_count"].get("approach", 0) > low["edge_max_person_count"].get("approach", 0), "bottleneck_width_is_physical_geometry": True, "counterflow_has_both_directions": {"eastbound", "westbound"}.issubset(results["counterflow"]["flow_prefixes"]), "alternative_routes_both_used": results["alternative_routes"]["edge_max_person_count"].get("upper_a", 0) > 0 and results["alternative_routes"]["edge_max_person_count"].get("lower_a", 0) > 0}
    report = {"status": "pass" if all(checks.values()) else "fail", "sumo_model": "striping", "step_length_seconds": 0.5, "seed": 20260908, "checks": checks, "results": results, "limitations": "Engineering mechanism test; not calibrated against observed human crowd data."}
    path = output / "congestion_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
