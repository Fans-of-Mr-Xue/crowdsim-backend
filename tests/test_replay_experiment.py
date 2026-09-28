from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.infrastructure.decision_log_reader import DecisionLogReader, LEGACY_FORMAT
from crowdsim.infrastructure.decision_table_writer import FORMAT, FORMAT_VERSION
from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary
from scripts.replay_experiment import replay_experiment
from scripts.run_acceptance import replay_evidence

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "tests" / "scenarios" / "unidirectional_corridor"


def has_sumo():
    try:
        discover_sumo_binary()
        return True
    except FileNotFoundError:
        return False


@unittest.skipUnless(has_sumo(), "real SUMO binary is required")
class RealReplayTests(unittest.TestCase):
    def test_split_and_legacy_replay_match_without_new_decisions(self):
        runtime = SimulationRuntime(SCENARIO / "scenario.sumocfg", pedestrian_route_files=[SCENARIO / "demand.rou.xml"])
        try:
            runtime.initialize()
            runtime.start()
            for _ in range(24):
                runtime.tick()
        finally:
            runtime.close()
        source = runtime.recorder.directory
        source_stat = (source / "decisions.jsonl").stat()
        with patch("crowdsim.decision.agent_decision.AgentDecisionEngine.rule_plan", side_effect=AssertionError("fresh rules forbidden")):
            report = replay_experiment(source)
        self.assertEqual("pass", report["status"], report)
        self.assertEqual(FORMAT, report["input_format"])
        self.assertTrue(report["population_match"])
        self.assertEqual(0, report["max_position_error_m"])
        self.assertGreater(report["recorded_decisions"], 0)
        self.assertNotEqual(str(source), report["replay_run_directory"])
        self.assertEqual(source_stat.st_mtime_ns, (source / "decisions.jsonl").stat().st_mtime_ns)

        with tempfile.TemporaryDirectory() as temporary:
            legacy = Path(temporary) / "legacy"
            shutil.copytree(source, legacy)
            manifest_path = legacy / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            for name in list(manifest):
                if name.startswith("decision_log_"):
                    del manifest[name]
            manifest_path.write_text(json.dumps(manifest))
            (legacy / "decision_manifest.json").unlink()
            with DecisionLogReader(source) as reader, (legacy / "decisions.jsonl").open("w") as handle:
                for plan in reader.iter_plans():
                    handle.write(json.dumps({"plan": asdict(plan)}) + "\n")
            with patch("crowdsim.decision.agent_decision.AgentDecisionEngine.rule_plan", side_effect=AssertionError("fresh rules forbidden")):
                old_report = replay_experiment(legacy)
            self.assertEqual("pass", old_report["status"], old_report)
            self.assertEqual(LEGACY_FORMAT, old_report["input_format"])
            self.assertEqual(report["recorded_decisions"], old_report["recorded_decisions"])


class ReplayEvidenceTests(unittest.TestCase):
    def evidence(self, root, kind, suffix):
        source = root / "runs" / f"run-source-{suffix}"
        source.mkdir(parents=True)
        manifest = {"run_id": source.name}
        if kind == FORMAT:
            manifest.update(decision_log_format=FORMAT, decision_log_version=FORMAT_VERSION)
        (source / "manifest.json").write_text(json.dumps(manifest))
        filenames = ["decisions.jsonl", "trajectory.csv", "demand.rou.xml", "summary.json", "profiles.jsonl"]
        if kind == FORMAT:
            filenames += ["decision_plans.jsonl", "decision_routes.jsonl"]
            (source / "decision_manifest.json").write_text("{}")
        for name in filenames:
            (source / name).touch()
        report = {
            "report_version": 2, "status": "pass", "llm_called": False, "fresh_decisions_disabled": True,
            "input_format": kind, "input_format_version": FORMAT_VERSION if kind == FORMAT else 0,
            "source_run_directory": str(source), "source_run_id": source.name,
            "source_manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
            "input_file_stats": {name: {"bytes": (source / name).stat().st_size, "mtime_ns": (source / name).stat().st_mtime_ns} for name in filenames},
        }
        if kind == FORMAT:
            report["source_decision_manifest_sha256"] = hashlib.sha256((source / "decision_manifest.json").read_bytes()).hexdigest()
        output = root / "runs" / f"run-report-{suffix}"
        output.mkdir()
        (output / "replay_report.json").write_text(json.dumps(report))
        return source, output

    def test_legacy_success_cannot_certify_split_and_new_success_can(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.evidence(root, LEGACY_FORMAT, "old")
            self.assertEqual("partial", replay_evidence(root)["status"])
            self.evidence(root, FORMAT, "new")
            result = replay_evidence(root)
            self.assertEqual("pass", result["status"])
            self.assertEqual({LEGACY_FORMAT, FORMAT}, set(result["by_format"]))

    def test_changed_input_and_unidentified_old_report_are_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = self.evidence(root, FORMAT, "new")
            (source / "decisions.jsonl").write_text("changed")
            self.assertEqual("partial", replay_evidence(root)["status"])
            (output / "replay_report.json").write_text('{"status":"pass"}')
            self.assertEqual(1, replay_evidence(root)["ignored_reports"])

    def test_missing_replay_inputs_fail_without_starting_sumo(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(SimulationRuntime, "initialize", side_effect=AssertionError("must not start")):
                report = replay_experiment(temporary)
            self.assertEqual("fail", report["status"])
            self.assertIn("missing", report)


if __name__ == "__main__":
    unittest.main()
