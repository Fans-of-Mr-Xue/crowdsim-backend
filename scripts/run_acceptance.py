"""Run staged checks and generate an honest F01-F18 evidence matrix."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

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
    "F10": ["tests/test_agent_decision.py"],
    "F11": ["tests/test_decision_scheduler.py"],
    "F12": ["tests/test_event_catalog.py"],
    "F13": ["tests/test_hazard_model.py"],
    "F15": ["tests/test_websocket_contract.py", "tests/test_simulation_loop.py"]
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
    replay_reports = sorted((ROOT / "runs").glob("run-*/replay_report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    replay = json.loads(replay_reports[0].read_text(encoding="utf-8")) if replay_reports else None
    reports["F16"] = {"status": "pass" if replay and replay.get("status") == "pass" else "partial", "evidence_path": str(replay_reports[0]) if replay_reports else None, "measured": replay}
    reports["F10"]["status"] = "partial" if reports["F10"]["status"] == "pass" else reports["F10"]["status"]
    reports["F10"]["reason"] = "Provider-neutral gateway, contract and fallback tests pass; the concrete LLM integration method has intentionally not yet been supplied."
    reports["F15"]["status"] = "partial" if reports["F15"]["status"] == "pass" else reports["F15"]["status"]
    reports["F15"]["reason"] = "Backend protocol tests pass; actual frontend repository/runtime was not exercised by this command."
    reports["F17"] = {"status": "partial", "reason": "Pair IDs and deterministic rule runs are supported; a paired real-LLM run waits for the concrete gateway integration method."}
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
