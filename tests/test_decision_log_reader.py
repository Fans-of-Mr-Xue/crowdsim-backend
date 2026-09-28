from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from crowdsim.domain.crowdsim_models import BehaviorPlan
from crowdsim.infrastructure.decision_log_reader import DecisionLogError, DecisionLogReader, LEGACY_FORMAT
from crowdsim.infrastructure.decision_table_writer import FORMAT, FORMAT_VERSION, DecisionTableWriter
from scripts.replay_experiment import BatchCursor, iter_trajectory_batches


class DecisionLogReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def split(self, plans, *, current=None, budgets=None):
        directory = self.root / "split"
        writer = DecisionTableWriter(directory, cache_budgets=budgets)
        for plan in plans:
            writer.write_record({"plan": plan, "execution": {"status": "rejected"},
                                 "context": {"state": {"current_plan": current}, "profile": {"id": "001"}}})
        writer.close()
        (directory / "manifest.json").write_text(json.dumps({
            "decision_log_format": FORMAT, "decision_log_version": FORMAT_VERSION,
            "decision_log_replay_supported": False}))
        return directory

    def legacy(self, plans):
        directory = self.root / "legacy"
        directory.mkdir()
        (directory / "manifest.json").write_text("{}")
        with (directory / "decisions.jsonl").open("w") as handle:
            for plan in plans:
                handle.write(json.dumps({"plan": asdict(plan), "context": {"large": "ignored"}}) + "\n")
        return directory

    def test_new_and_old_formats_produce_identical_plans_and_batches(self):
        plans = [BehaviorPlan("001", "snapshot", "change_goal", route_edges=("a", "b", "a"),
                              next_route_edges=("b", "a"), arrival_position=-1.0, decided_at=time)
                 for time in (0, 0, 2)]
        for directory, kind in ((self.legacy(plans), LEGACY_FORMAT), (self.split(plans), FORMAT)):
            with DecisionLogReader(directory) as reader:
                self.assertEqual(kind, reader.format)
                self.assertEqual(plans, list(reader.iter_plans()))
                self.assertEqual([(0, plans[:2]), (2, plans[2:])], list(reader.iter_plan_batches()))
            self.assertTrue(all(handle.closed for handle in reader._handles.values()))

    def test_current_plan_is_not_replayed_and_cache_eviction_keeps_refs_valid(self):
        proposed = BehaviorPlan("001", "s", "continue")
        previous = BehaviorPlan("001", "s", "wait", wait_until=2)
        directory = self.split([proposed, proposed], current=previous, budgets={"plans": 0})
        with DecisionLogReader(directory, cache_bytes=0) as reader:
            self.assertEqual([proposed, proposed], list(reader.iter_plans()))
            self.assertEqual(4, len(reader.offsets["plans"]))
            self.assertEqual(8, reader.offsets["plans"].itemsize)
            self.assertEqual(0, reader._cache_size)

    def test_unknown_format_or_version_rejected(self):
        directory = self.legacy([])
        for fields in ({"decision_log_format": "unknown"}, {"decision_log_format": FORMAT, "decision_log_version": 99}):
            (directory / "manifest.json").write_text(json.dumps(fields))
            with self.assertRaisesRegex(DecisionLogError, "unsupported"):
                DecisionLogReader(directory)

    def test_missing_table_and_modified_table_rejected(self):
        directory = self.split([BehaviorPlan("001", "s", "continue")])
        route = directory / "decision_routes.jsonl"
        route.unlink()
        with self.assertRaisesRegex(DecisionLogError, "missing decision table"):
            DecisionLogReader(directory)
        route.write_text("corrupted\n")
        with self.assertRaises(DecisionLogError):
            DecisionLogReader(directory)

    def test_main_checksum_and_incomplete_run_rejected(self):
        directory = self.split([BehaviorPlan("001", "s", "continue")])
        with (directory / "decisions.jsonl").open("ab") as handle:
            handle.write(b"{}\n")
        with self.assertRaisesRegex(DecisionLogError, "checksum mismatch"):
            DecisionLogReader(directory)
        manifest = directory / "decision_manifest.json"
        data = json.loads(manifest.read_text())
        data["status"] = "aborted"
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(DecisionLogError, "not a completed"):
            DecisionLogReader(directory)

    def test_invalid_reference_and_unsupported_plan_fields_rejected(self):
        directory = self.split([BehaviorPlan("001", "s", "continue")])
        with DecisionLogReader(directory) as reader:
            for reference in ({"$ref": "plans", "id": 999}, {"$ref": "routes", "id": 0}, {"$ref": "plans", "id": True}):
                with self.assertRaises(DecisionLogError):
                    reader._lookup(reference, "plans")
        legacy = self.legacy([])
        (legacy / "decisions.jsonl").write_text(json.dumps({"plan": asdict(BehaviorPlan("001", "s", "continue")) | {"unknown": 1}}) + "\n")
        with DecisionLogReader(legacy) as reader:
            with self.assertRaisesRegex(DecisionLogError, "unsupported BehaviorPlan"):
                list(reader.iter_plans())

    def test_decision_order_invalid_json_and_closed_reader(self):
        directory = self.legacy([BehaviorPlan("001", "s", "continue", decided_at=2), BehaviorPlan("001", "s", "continue", decided_at=0)])
        with DecisionLogReader(directory) as reader:
            with self.assertRaisesRegex(DecisionLogError, "out of order"):
                list(reader.iter_plan_batches())
        with self.assertRaises(RuntimeError):
            list(reader.iter_plans())
        (directory / "decisions.jsonl").write_text("{broken\n")
        with DecisionLogReader(directory) as reader:
            with self.assertRaisesRegex(DecisionLogError, "invalid JSON"):
                list(reader.iter_plans())


class StreamingTrajectoryTests(unittest.TestCase):
    def test_batches_preserve_person_ids_and_detect_duplicates_and_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trajectory.csv"
            header = "time,person_id,x,y,speed\n"
            path.write_text(header + "0.5,001,1,2,1.1\n0.5,1,2,3,1.2\n1,001,3,4,1.1\n")
            cursor = BatchCursor(iter_trajectory_batches(path))
            self.assertEqual({}, cursor.take(0, {}))
            self.assertEqual({"001", "1"}, set(cursor.take(0.5, {})))
            self.assertEqual({"001"}, set(cursor.take(1, {})))
            self.assertIsNone(cursor.pending)
            cursor.close()
            for content in ("1,001,1,2,1\n0.5,1,2,3,1\n", "0.5,001,1,2,1\n0.5,001,1,2,1\n", "0.5,001,nan,2,1\n"):
                path.write_text(header + content)
                with self.assertRaises(DecisionLogError):
                    list(iter_trajectory_batches(path))

    def test_unconsumed_batch_is_not_silently_skipped(self):
        cursor = BatchCursor(iter([(1, ["plan"])]))
        with self.assertRaises(DecisionLogError):
            cursor.take(2, [])


if __name__ == "__main__":
    unittest.main()
