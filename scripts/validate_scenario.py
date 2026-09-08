"""Audit a SUMO scenario and prove that the real SUMO binary can run it.

This is the stage-1 gate from docs/SUMO原生行人模型完整改造方案.md.  It is
deliberately independent from the application runtime so a broken scenario is
distinguishable from a later TraCI/runtime defect.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "scenarios" / "shanghai_bund" / "bund.research.sumocfg"
MICRO_SCENARIOS = (
    "unidirectional_corridor",
    "bottleneck",
    "counterflow",
    "alternative_routes",
)
EXPECTED_SUMO_VERSION = "1.24.0"


def _discover_binary(name: str, explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_binary = os.environ.get(f"{name.upper()}_BINARY")
    if env_binary:
        candidates.append(Path(env_binary))
    sumo_home = os.environ.get("SUMO_HOME")
    if sumo_home:
        suffix = ".exe" if os.name == "nt" else ""
        candidates.append(Path(sumo_home) / "bin" / f"{name}{suffix}")
    on_path = shutil.which(name)
    if on_path:
        candidates.append(Path(on_path))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        f"Cannot find {name}. Set SUMO_HOME or {name.upper()}_BINARY."
    )


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def sumo_version(sumo_binary: Path) -> str | None:
    result = _run([str(sumo_binary), "--version"], PROJECT_ROOT)
    match = re.search(r"Version\s+([^\s]+)", result.stdout + result.stderr)
    return match.group(1) if match else None


def build_micro_scenario(directory: Path, netconvert_binary: Path) -> dict[str, object]:
    nodes = directory / "nodes.nod.xml"
    edges = directory / "edges.edg.xml"
    network = directory / "network.net.xml"
    command = [
        str(netconvert_binary),
        "--node-files",
        str(nodes),
        "--edge-files",
        str(edges),
        "--output-file",
        str(network),
        "--no-turnarounds",
        "true",
    ]
    result = _run(command, directory)
    return {
        "status": "pass" if result.returncode == 0 and network.is_file() else "fail",
        "command": command,
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _config_value(root: ET.Element, section: str, option: str) -> str | None:
    element = root.find(f"./{section}/{option}")
    return None if element is None else element.get("value")


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in {"true", "1", "yes"}


def audit_config(config_path: Path) -> dict[str, object]:
    root = ET.parse(config_path).getroot()
    begin = float(_config_value(root, "time", "begin") or 0.0)
    end = float(_config_value(root, "time", "end") or 0.0)
    duration = end - begin
    jamtimes = {
        name: float(_config_value(root, "processing", f"pedestrian.{name}") or 0.0)
        for name in (
            "striping.jamtime",
            "striping.jamtime.crossing",
            "striping.jamtime.narrow",
        )
    }
    checks = {
        "pedestrian_model_is_striping": _config_value(root, "processing", "pedestrian.model") == "striping",
        "step_length_is_0_5_seconds": float(_config_value(root, "time", "step-length") or -1) == 0.5,
        "route_errors_are_strict": _as_bool(_config_value(root, "processing", "ignore-route-errors")) is False,
        "vehicle_teleport_is_disabled": float(_config_value(root, "processing", "time-to-teleport") or 0) == -1,
        "jamtime_exceeds_experiment": all(value > duration for value in jamtimes.values()),
        "seed_is_explicit": _config_value(root, "random_number", "seed") is not None,
    }
    return {
        "begin_seconds": begin,
        "end_seconds": end,
        "duration_seconds": duration,
        "step_length_seconds": float(_config_value(root, "time", "step-length") or 0.0),
        "pedestrian_model": _config_value(root, "processing", "pedestrian.model"),
        "ignore_route_errors": _as_bool(_config_value(root, "processing", "ignore-route-errors")),
        "jamtime_seconds": jamtimes,
        "checks": checks,
        "status": "pass" if all(checks.values()) else "fail",
    }


def _resolved_input_files(config_path: Path, option: str) -> list[Path]:
    root = ET.parse(config_path).getroot()
    raw = _config_value(root, "input", option) or ""
    return [(config_path.parent / value.strip()).resolve() for value in raw.split(",") if value.strip()]


def audit_network(config_path: Path) -> dict[str, object]:
    network_files = _resolved_input_files(config_path, "net-file")
    if len(network_files) != 1 or not network_files[0].is_file():
        return {"status": "fail", "error": "Exactly one readable net-file is required."}
    network_path = network_files[0]
    edge_ids: set[str] = set()
    function_counts: dict[str, int] = {}
    lane_count = 0
    pedestrian_lane_count = 0
    location: dict[str, str] | None = None
    for _, element in ET.iterparse(network_path, events=("end",)):
        if element.tag == "location":
            location = dict(element.attrib)
        elif element.tag == "lane":
            lane_count += 1
            allowed = set((element.get("allow") or "").split())
            disallowed = set((element.get("disallow") or "").split())
            if "pedestrian" in allowed or (not allowed and "pedestrian" not in disallowed):
                pedestrian_lane_count += 1
        elif element.tag == "edge":
            edge_id = element.get("id")
            if edge_id:
                edge_ids.add(edge_id)
            function = element.get("function", "normal")
            function_counts[function] = function_counts.get(function, 0) + 1
        element.clear()

    referenced_edges: set[str] = set()
    person_count = 0
    person_flow_count = 0
    vehicle_count = 0
    duplicate_ids: set[str] = set()
    seen_ids: dict[str, set[str]] = {"person": set(), "personFlow": set(), "vehicle": set()}
    route_files = _resolved_input_files(config_path, "route-files")
    unreadable_routes = [str(path) for path in route_files if not path.is_file()]
    for route_path in route_files:
        if not route_path.is_file():
            continue
        for _, element in ET.iterparse(route_path, events=("end",)):
            if element.tag in {"person", "personFlow", "vehicle"}:
                entity_id = element.get("id")
                if entity_id:
                    if entity_id in seen_ids[element.tag]:
                        duplicate_ids.add(entity_id)
                    seen_ids[element.tag].add(entity_id)
                if element.tag == "person":
                    person_count += 1
                elif element.tag == "personFlow":
                    person_flow_count += 1
                else:
                    vehicle_count += 1
            elif element.tag in {"walk", "route"}:
                referenced_edges.update((element.get("edges") or "").split())
            element.clear()
    missing_edges = sorted(referenced_edges - edge_ids)
    checks = {
        "all_input_files_exist": not unreadable_routes,
        "network_has_edges": bool(edge_ids),
        "network_has_pedestrian_lanes": pedestrian_lane_count > 0,
        "all_explicit_route_edges_exist": not missing_edges,
        "demand_ids_are_unique": not duplicate_ids,
    }
    return {
        "network_file": str(network_path),
        "route_files": [str(path) for path in route_files],
        "edge_count": len(edge_ids),
        "edge_function_counts": function_counts,
        "lane_count": lane_count,
        "pedestrian_lane_count": pedestrian_lane_count,
        "location": location,
        "demand": {
            "persons": person_count,
            "person_flows": person_flow_count,
            "vehicles": vehicle_count,
        },
        "missing_route_edges": missing_edges[:100],
        "missing_route_edge_count": len(missing_edges),
        "duplicate_ids": sorted(duplicate_ids)[:100],
        "unreadable_route_files": unreadable_routes,
        "checks": checks,
        "status": "pass" if all(checks.values()) else "fail",
    }


def runtime_check(config_path: Path, sumo_binary: Path, duration: float) -> dict[str, object]:
    config_audit = audit_config(config_path)
    run_end = min(
        float(config_audit["end_seconds"]),
        float(config_audit["begin_seconds"]) + duration,
    )
    with tempfile.TemporaryDirectory(prefix="crowdsim-sumo-audit-") as temp_name:
        temp_dir = Path(temp_name)
        log_path = temp_dir / "sumo.log"
        summary_path = temp_dir / "summary.xml"
        fcd_path = temp_dir / "fcd.xml"
        command = [
            str(sumo_binary),
            "-c",
            str(config_path),
            "--end",
            str(run_end),
            "--log",
            str(log_path),
            "--summary-output",
            str(summary_path),
            "--fcd-output",
            str(fcd_path),
            "--verbose",
            "true",
            "--duration-log.statistics",
            "true",
            "--no-step-log",
            "true",
        ]
        result = _run(command, config_path.parent)
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        summary_last: dict[str, str] = {}
        if summary_path.is_file():
            for _, element in ET.iterparse(summary_path, events=("end",)):
                if element.tag == "step":
                    summary_last = dict(element.attrib)
                element.clear()
        warning_lines = [line for line in log_text.splitlines() if "warning" in line.lower()]
        error_lines = [line for line in log_text.splitlines() if "error" in line.lower()]
        person_match = re.search(
            r"Persons:\s*\n\s*Inserted:\s*(\d+)(?:\s*\(Loaded:\s*(\d+)\))?\s*\n\s*Running:\s*(\d+)",
            log_text,
        )
        person_stats = {
            "inserted": int(person_match.group(1)) if person_match else None,
            "loaded": int(person_match.group(2)) if person_match and person_match.group(2) else None,
            "running": int(person_match.group(3)) if person_match else None,
        }
        first_position: dict[str, tuple[float, float]] = {}
        last_position: dict[str, tuple[float, float]] = {}
        person_sample_count = 0
        if fcd_path.is_file():
            for _, element in ET.iterparse(fcd_path, events=("end",)):
                if element.tag == "person" and element.get("id") is not None:
                    person_id = element.get("id")
                    assert person_id is not None
                    position = (float(element.get("x", "nan")), float(element.get("y", "nan")))
                    first_position.setdefault(person_id, position)
                    last_position[person_id] = position
                    person_sample_count += 1
                element.clear()
        moving_person_ids = sorted(
            person_id
            for person_id, start in first_position.items()
            if last_position.get(person_id) != start
        )
        movement_proven = bool(moving_person_ids)
        return {
            "status": "pass" if result.returncode == 0 and not error_lines and movement_proven else "fail",
            "command": command,
            "exit_code": result.returncode,
            "simulated_until_seconds": run_end,
            "last_summary_step": summary_last,
            "persons": person_stats,
            "fcd_person_sample_count": person_sample_count,
            "moving_person_count": len(moving_person_ids),
            "moving_person_examples": moving_person_ids[:10],
            "movement_proven": movement_proven,
            "warning_count": len(warning_lines),
            "warnings": warning_lines[:50],
            "error_count": len(error_lines),
            "errors": error_lines[:50],
            "stdout_tail": result.stdout.splitlines()[-20:],
            "stderr_tail": result.stderr.splitlines()[-20:],
        }


def validate_one(config_path: Path, sumo_binary: Path, duration: float) -> dict[str, object]:
    config_path = config_path.resolve()
    config_result = audit_config(config_path)
    network_result = audit_network(config_path)
    runtime_result = runtime_check(config_path, sumo_binary, duration)
    status = "pass" if all(
        part["status"] == "pass"
        for part in (config_result, network_result, runtime_result)
    ) else "fail"
    return {
        "status": status,
        "config_path": str(config_path),
        "config": config_result,
        "network_and_demand": network_result,
        "runtime": runtime_result,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--sumo-binary")
    parser.add_argument("--netconvert-binary")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--all", action="store_true", help="Also rebuild and validate all four micro scenarios.")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        sumo_binary = _discover_binary("sumo", args.sumo_binary)
        netconvert_binary = _discover_binary("netconvert", args.netconvert_binary) if args.all else None
    except FileNotFoundError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    version = sumo_version(sumo_binary)
    report: dict[str, object] = {
        "stage": 1,
        "status": "pass",
        "sumo_binary": str(sumo_binary),
        "sumo_version": version,
        "expected_sumo_version": EXPECTED_SUMO_VERSION,
        "version_matches": version == EXPECTED_SUMO_VERSION,
        "scenarios": {},
    }
    scenarios = report["scenarios"]
    assert isinstance(scenarios, dict)
    scenarios["shanghai_bund"] = validate_one(args.config, sumo_binary, args.duration)

    if args.all:
        assert netconvert_binary is not None
        for name in MICRO_SCENARIOS:
            directory = PROJECT_ROOT / "tests" / "scenarios" / name
            build = build_micro_scenario(directory, netconvert_binary)
            validation = validate_one(
                directory / "scenario.sumocfg",
                sumo_binary,
                min(args.duration, 20.0),
            ) if build["status"] == "pass" else {"status": "fail"}
            scenarios[name] = {"build": build, **validation}

    report["status"] = "pass" if (
        report["version_matches"]
        and all(result["status"] == "pass" for result in scenarios.values())
    ) else "fail"
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output_path = args.output.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
