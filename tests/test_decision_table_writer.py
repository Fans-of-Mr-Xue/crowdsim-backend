import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, BehaviorPlan
from crowdsim.infrastructure.decision_table_writer import DecisionTableWriter, TABLE_FILES, MANIFEST_FILE
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder


def sample():
    plan = {
        "person_id": "001", "proposed_action": "visit", "route_edges": ["a", "b", "a"],
        "next_route_edges": ["b", "a"], "reason": "前往广场", "confidence": 0.9,
        "arrival_position": -0.0, "next_arrival_position": None,
    }
    return {
        "context": {
            "profile": {"person_id": "001", "nationality": "CN", "future_field": {"x": None}},
            "state": {"person_id": "001", "stress": 0.3, "current_plan": copy.deepcopy(plan)},
            "observation": {"neighbour_ids": ["001", "1", "001"], "local_people_count": 3},
        },
        "candidates": [{"edges": ["a", "b", "a"], "cost": 1.0, "extra": [None, "中文"]}],
        "plan": plan, "execution": {"status": "accepted", "custom": {"id": 17}},
        "unknown_root": {"$ref": "not_storage", "id": 99},
    }


class DecisionTableWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.writer = DecisionTableWriter(self.directory)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def rows(self, table):
        self.writer.flush()
        return [json.loads(line) for line in (self.directory / TABLE_FILES[table]).read_text().splitlines()]

    def test_shared_values_and_every_remaining_field_are_preserved(self):
        original = sample()
        self.writer.write_record(original)
        self.writer.write_record(original)
        records = self.rows("decisions")
        self.assertEqual([0, 1], [row["id"] for row in records])
        stored = records[0]["value"]
        self.assertEqual(stored, records[1]["value"])
        self.assertEqual(original["execution"], stored["execution"])
        self.assertEqual(original["unknown_root"], stored["unknown_root"])
        self.assertEqual(original["context"]["profile"], self.rows("profiles")[0]["value"])
        self.assertEqual(stored["plan"], stored["context"]["state"]["current_plan"])
        self.assertEqual(1, len(self.rows("plans")))
        plan = self.rows("plans")[0]["value"]
        self.assertEqual(original["plan"]["reason"], plan["reason"])
        self.assertEqual(-0.0, plan["arrival_position"])
        self.assertIsNone(plan["next_arrival_position"])
        self.assertEqual(plan["route_edges"], stored["candidates"][0]["edges"])
        self.assertEqual(original["candidates"][0]["extra"], stored["candidates"][0]["extra"])
        self.assertIsInstance(stored["candidates"][0]["cost"], float)
        routes = self.rows("routes")
        self.assertEqual([["a", "b", "a"], ["b", "a"]], [row["value"] for row in routes])
        self.assertEqual(["001", "1"], [row["value"] for row in self.rows("person_ids")])
        self.assertEqual({"encoding": "person_ids", "ids": [0, 1, 0]}, self.rows("neighbors")[0]["value"])
        self.assertEqual(1, len(self.rows("neighbors")))
        self.assertEqual(original, sample())  # Caller data was not mutated.

    def test_profile_versions_and_rejected_current_plan_remain_distinct(self):
        first = sample()
        self.writer.write_record(first)
        second = sample()
        second["context"]["profile"]["nationality"] = "FR"
        second["plan"]["reason"] = "新计划被拒绝"
        second["execution"]["status"] = "rejected"
        self.writer.write_record(second)
        records = self.rows("decisions")
        self.assertNotEqual(records[0]["value"]["context"]["profile"], records[1]["value"]["context"]["profile"])
        self.assertNotEqual(records[1]["value"]["plan"], records[1]["value"]["context"]["state"]["current_plan"])
        self.assertEqual(records[0]["value"]["plan"], records[1]["value"]["context"]["state"]["current_plan"])
        self.assertEqual(2, len(self.rows("profiles")))

    def test_empty_null_and_missing_values_remain_inline(self):
        records = [
            {"plan": None, "candidates": [], "context": None},
            {"context": {"state": {"current_plan": None}, "observation": {"neighbour_ids": []}}, "plan": {"route_edges": []}},
            {"context": {"observation": {"neighbour_ids": None}}, "other": []},
            {},
        ]
        for record in records:
            self.writer.write_record(record)
        stored = [row["value"] for row in self.rows("decisions")]
        self.assertEqual(records[0], stored[0])
        self.assertEqual(records[1]["context"], stored[1]["context"])
        self.assertEqual([], self.rows("plans")[0]["value"]["route_edges"])
        self.assertEqual(records[2:], stored[2:])
        self.assertEqual([], self.rows("routes"))
        self.assertEqual([], self.rows("neighbors"))

    def test_freezes_dataclasses_at_record_time(self):
        plan = BehaviorPlan("001", "snapshot", "continue")
        state = AgentState("001", current_plan=plan)
        profile = AgentProfile("001", information_trust={"friend": 0.8})
        self.writer.write_record({"context": {"profile": profile, "state": state}, "plan": plan})
        state.stress = 0.95
        profile.information_trust["friend"] = 0.0
        self.assertEqual(0.0, self.rows("decisions")[0]["value"]["context"]["state"]["stress"])
        self.assertEqual(0.8, self.rows("profiles")[0]["value"]["information_trust"]["friend"])
        self.assertEqual(asdict(plan) | {"route_edges": [], "next_route_edges": []}, self.rows("plans")[0]["value"])

    def test_numeric_types_and_non_string_neighbors_are_not_coerced(self):
        for number in (1, 1.0, -0.0):
            self.writer.write_record({"context": {"profile": {"value": number}, "observation": {"neighbour_ids": ["001", 1, None]}}})
        values = [row["value"]["value"] for row in self.rows("profiles")]
        self.assertEqual([int, float, float], [type(value) for value in values])
        self.assertEqual({"encoding": "original", "ids": ["001", 1, None]}, self.rows("neighbors")[0]["value"])
        self.assertEqual([], self.rows("person_ids"))

    def test_cache_eviction_only_adds_rows_and_keeps_old_references(self):
        self.writer.close()
        self.writer = DecisionTableWriter(self.directory / "bounded", cache_budgets={"profiles": 180})
        self.directory = self.directory / "bounded"
        for value in ("a", "b", "a"):
            self.writer.write_record({"context": {"profile": {"value": value}}})
        refs = [row["value"]["context"]["profile"]["id"] for row in self.rows("decisions")]
        self.assertEqual([0, 1, 2], refs)
        self.assertEqual(["a", "b", "a"], [row["value"]["value"] for row in self.rows("profiles")])
        self.assertGreater(self.writer.caches["profiles"].evictions, 0)
        self.assertLessEqual(self.writer.caches["profiles"].bytes, 180)

    def test_manifest_counts_sizes_and_checksums_and_idempotent_close(self):
        self.writer.write_record(sample())
        self.writer.close()
        self.writer.close(aborted=True)
        manifest = json.loads((self.directory / MANIFEST_FILE).read_text())
        self.assertEqual("complete", manifest["status"])
        self.assertTrue(manifest["replay_supported"])
        for table, filename in TABLE_FILES.items():
            data = (self.directory / filename).read_bytes()
            self.assertEqual(len(data), manifest["tables"][table]["bytes"])
            self.assertEqual(len(data.splitlines()), manifest["tables"][table]["rows"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest["tables"][table]["sha256"])
            self.assertTrue(self.writer.handles[table].closed)
        with self.assertRaises(RuntimeError):
            self.writer.write_record(sample())

    def test_existing_logs_are_never_overwritten(self):
        self.writer.write_record(sample())
        self.writer.close()
        previous = (self.directory / "decisions.jsonl").read_bytes()
        with self.assertRaises(FileExistsError):
            DecisionTableWriter(self.directory)
        self.assertEqual(previous, (self.directory / "decisions.jsonl").read_bytes())

    def test_serialization_error_writes_no_partial_record(self):
        with self.assertRaises(TypeError):
            self.writer.write_record({"unsupported": object()})
        self.assertEqual(0, sum(self.writer.rows.values()))
        self.writer.write_record({"valid": True})
        self.assertEqual(1, self.writer.rows["decisions"])

    def test_record_count_and_time_trigger_flush(self):
        self.writer.flush_every_records = 2
        with patch("crowdsim.infrastructure.decision_table_writer.time.monotonic", return_value=self.writer.last_flush):
            self.writer.write_record({})
            self.assertEqual(0, self.writer.flushes)
            self.writer.write_record({})
            self.assertEqual(1, self.writer.flushes)
        with patch("crowdsim.infrastructure.decision_table_writer.time.monotonic", return_value=self.writer.last_flush + 2):
            self.writer.write_record({})
            self.assertEqual(2, self.writer.flushes)

    def test_flush_failure_still_closes_all_files_and_marks_aborted(self):
        self.writer.write_record(sample())
        with patch.object(self.writer, "flush", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.writer.close()
        self.assertTrue(all(handle.closed for handle in self.writer.handles.values()))
        self.assertEqual("aborted", json.loads((self.directory / MANIFEST_FILE).read_text())["status"])

    def test_short_write_marks_failure_and_rejects_further_records(self):
        with patch.object(self.writer.handles["decisions"], "write", return_value=0):
            with self.assertRaisesRegex(OSError, "short write"):
                self.writer.write_record(sample())
        self.assertTrue(self.writer.failed)
        self.assertEqual(0, self.writer.rows["decisions"])
        with self.assertRaises(RuntimeError):
            self.writer.write_record(sample())
        self.writer.close()
        self.assertEqual("aborted", self.writer.status)

    def test_repeated_content_reduces_total_six_table_storage(self):
        record = sample()
        legacy_bytes = 0
        for _ in range(128):
            legacy_bytes += len((json.dumps(record, ensure_ascii=False) + "\n").encode())
            self.writer.write_record(record)
        self.writer.close()
        table_bytes = sum((self.directory / filename).stat().st_size for filename in TABLE_FILES.values())
        self.assertLess(table_bytes, legacy_bytes)

    def test_open_failure_closes_already_opened_tables(self):
        opened = []
        real_open = Path.open

        def failing_open(path, *args, **kwargs):
            if path.name == TABLE_FILES["routes"]:
                raise OSError("cannot open route table")
            handle = real_open(path, *args, **kwargs)
            opened.append(handle)
            return handle

        with patch.object(Path, "open", failing_open):
            with self.assertRaises(OSError):
                DecisionTableWriter(self.directory / "open_failure")
        self.assertTrue(opened)
        self.assertTrue(all(handle.closed for handle in opened))


class ExperimentRecorderTableTests(unittest.TestCase):
    def test_recorder_integration_metadata_and_abort(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = Path(__file__).resolve()
            recorder = ExperimentRecorder("test", config, {"backend": "sumo"}, root=root)
            recorder.record_decision(BehaviorPlan("001", "snapshot", "continue"), {"status": "accepted"}, context={"profile": AgentProfile("001")})
            recorder.record_command({"type": "pause"}, {"status": "accepted"})
            recorder.record_messages([{"id": "message"}])
            recorder.abort("test failure")
            self.assertTrue(recorder.decision_writer.closed)
            self.assertEqual("aborted", recorder.decision_log_diagnostics["status"])
            self.assertEqual(1, recorder.decision_log_diagnostics["records"])
            manifest = json.loads((recorder.directory / "manifest.json").read_text())
            self.assertEqual("decision_tables_jsonl", manifest["decision_log_format"])
            self.assertTrue(manifest["decision_log_replay_supported"])
            for filename in ("commands.jsonl", "messages.jsonl", "profiles.jsonl", "lifecycle.jsonl"):
                self.assertTrue((recorder.directory / filename).exists())
            with self.assertRaises(FileExistsError):
                ExperimentRecorder("test", config, {}, root=root)

    def test_finalize_closes_writer_before_summary_is_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = ExperimentRecorder("test", Path(__file__).resolve(), {}, root=Path(temporary))
            runtime = SimpleNamespace(last_error=None, diagnostics=lambda: {"decision_log": recorder.decision_log_diagnostics})
            recorder.finalize(runtime)
            summary = json.loads((recorder.directory / "summary.json").read_text())
            self.assertTrue(summary["decision_log"]["closed"])
            self.assertEqual("complete", summary["decision_log"]["status"])

    def test_constructor_failure_closes_the_created_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            writers = []

            def create_writer(directory):
                writer = DecisionTableWriter(directory)
                writers.append(writer)
                return writer

            with patch("crowdsim.infrastructure.experiment_recorder.DecisionTableWriter", side_effect=create_writer):
                with patch.object(ExperimentRecorder, "_write_json", side_effect=OSError("manifest failure")):
                    with self.assertRaisesRegex(OSError, "manifest failure"):
                        ExperimentRecorder("test", Path(__file__).resolve(), {}, root=Path(temporary))
            self.assertEqual(1, len(writers))
            self.assertTrue(writers[0].closed)
            self.assertEqual("aborted", writers[0].status)


if __name__ == "__main__":
    unittest.main()
