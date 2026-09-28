"""Run staged checks and generate an honest F01-F18 evidence matrix."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crowdsim.infrastructure.decision_log_reader import LEGACY_FORMAT
from crowdsim.infrastructure.decision_table_writer import FORMAT, FORMAT_VERSION

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


TEST_MAP = {
    "F01": ["tests/test_sumo_adapter_integration.py"],
    "F02": ["tests/test_population_conservation.py"],
    "F03": ["tests/test_population_profiles.py"],
    "F04": ["tests/test_crowd_environment.py"],
    "F05": ["tests/test_state_updater.py"],
    "F06": ["tests/test_information_model.py"],
    "F07": ["tests/test_activity_planner.py", "tests/test_plan_executor_integration.py"],
    "F08": ["tests/test_plan_executor_integration.py"],
    "F09": ["tests/test_group_manager.py"],
    "F10": ["tests/test_agent_decision.py", "pedestrian_decision_skill/tests/test_skill.py", "tests/test_skill_integration.py"],
    "F11": ["tests/test_decision_scheduler.py"],
    "F12": ["tests/test_event_catalog.py"],
    "F13": ["tests/test_hazard_model.py"],
    "F15": ["tests/test_websocket_contract.py", "tests/test_simulation_loop.py"]
}


def replay_evidence(root):
    """Separate old/new evidence; an old success cannot certify split replay."""
    by_format = {}
    ignored = 0
    for path in sorted((root / "runs").glob("run-*/replay_report.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            kind = report.get("input_format")
            expected_version = {LEGACY_FORMAT: 0, FORMAT: FORMAT_VERSION}.get(kind)
            if (report.get("report_version") != 2 or expected_version is None
                    or report.get("input_format_version") != expected_version
                    or not report.get("fresh_decisions_disabled") or report.get("llm_called") is not False):
                raise ValueError("unidentified/unsupported replay evidence")
            source = Path(report["source_run_directory"])
            manifest_bytes = (source / "manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            source_kind = manifest.get("decision_log_format", LEGACY_FORMAT)
            if (source_kind != kind or manifest.get("run_id") != report.get("source_run_id")
                    or hashlib.sha256(manifest_bytes).hexdigest() != report.get("source_manifest_sha256")):
                raise ValueError("source run/manifest does not match report")
            if kind == FORMAT:
                if (manifest.get("decision_log_version") != FORMAT_VERSION
                        or hashlib.sha256((source / "decision_manifest.json").read_bytes()).hexdigest()
                        != report.get("source_decision_manifest_sha256")):
                    raise ValueError("source split manifest does not match report")
            stats = report.get("input_file_stats", {})
            required = {"decisions.jsonl", "trajectory.csv", "demand.rou.xml", "summary.json", "profiles.jsonl"}
            if (source / "commands.jsonl").is_file():
                required.add("commands.jsonl")
            if kind == FORMAT:
                required |= {"decision_plans.jsonl", "decision_routes.jsonl"}
            if not required.issubset(stats):
                raise ValueError("missing input file fingerprints")
            for name in required:
                current = (source / name).stat()
                if stats[name] != {"bytes": current.st_size, "mtime_ns": current.st_mtime_ns}:
                    raise ValueError("replay input changed after reporting")
            if kind not in by_format:
                by_format[kind] = {"status": report.get("status"), "evidence_path": str(path), "measured": report}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            ignored += 1
    latest_split = by_format.get(FORMAT)
    return {
        "status": latest_split["status"] if latest_split and latest_split["status"] in {"pass", "fail"} else "partial",
        "required_format": FORMAT, "by_format": by_format, "ignored_reports": ignored,
        "reason": "F16 requires identified split-format replay evidence; legacy success alone is insufficient. This is saved evidence, not a fresh replay run.",
    }


def run(command):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return {"exit_code": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}


def main() -> int:
    output = ROOT / "runs" / "acceptance_latest"
    output.mkdir(parents=True, exist_ok=True)
    reports = {}
    cache = {}
    for feature, tests in TEST_MAP.items():
        key = tuple(tests)
        if key not in cache:
            cache[key] = run([PYTHON, "-m", "pytest", "-q", *tests])
        result = cache[key]
        reports[feature] = {"status": "pass" if result["exit_code"] == 0 else "fail", "tests": tests, "evidence": result}
    scenario = run([PYTHON, "scripts/validate_scenario.py", "--all", "--duration", "30", "--output", str(output / "scenario_validation.json")])
    congestion = run([PYTHON, "scripts/run_congestion_experiments.py"])
    reports["F14"] = {"status": "pass" if scenario["exit_code"] == 0 and congestion["exit_code"] == 0 else "fail", "tests": ["scripts/validate_scenario.py --all", "scripts/run_congestion_experiments.py"], "reason": "Real striping control, high/low bottleneck, counterflow and alternative-route mechanisms passed; this is not human-data calibration.", "evidence": {"scenario": scenario, "congestion": congestion, "report": str(ROOT / "runs" / "congestion" / "congestion_report.json")}}
    replay_tests = run([PYTHON, "-m", "pytest", "-q", "tests/test_decision_log_reader.py", "tests/test_replay_experiment.py"])
    reports["F16"] = {**replay_evidence(ROOT), "tests": ["tests/test_decision_log_reader.py", "tests/test_replay_experiment.py"], "test_evidence": replay_tests}
    if replay_tests["exit_code"] != 0:
        reports["F16"]["status"] = "fail"
    reports["F10"]["status"] = "partial" if reports["F10"]["status"] == "pass" else reports["F10"]["status"]
    reports["F10"]["reason"] = "DeepSeek Skill, BehaviorPlan contract, fallback and SUMO integration tests pass; the paid live API request is intentionally excluded from automatic acceptance."
    reports["F15"]["status"] = "partial" if reports["F15"]["status"] == "pass" else reports["F15"]["status"]
    reports["F15"]["reason"] = "Backend protocol tests pass; actual frontend repository/runtime was not exercised by this command."
    reports["F17"] = {"status": "partial", "reason": "Pair IDs, deterministic rule runs and DeepSeek injection are supported; a paired paid real-LLM experiment has not yet been executed."}
    failures = [feature for feature, item in reports.items() if item["status"] == "fail"]
    partials = [feature for feature, item in reports.items() if item["status"] == "partial"]
    reports["F18"] = {"status": "fail" if failures else ("partial" if partials else "pass"), "reason": "Aggregate status preserves external and research-validation partials.", "failed_features": failures, "partial_features": partials}
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "python": PYTHON, "overall_status": reports["F18"]["status"], "features": reports}
    (output / "acceptance_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Acceptance report", "", f"Overall: {report['overall_status']}", "", "| Feature | Status |", "|---|---|"] + [f"| {feature} | {item['status']} |" for feature, item in sorted(reports.items())]
    (output / "acceptance_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"overall_status": report["overall_status"], "report": str(output / "acceptance_report.json"), "failed": failures, "partial": partials}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
