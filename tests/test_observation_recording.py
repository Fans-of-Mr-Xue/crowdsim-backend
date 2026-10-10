"""Exercise runtime wiring and actual files using a fake adapter, never SUMO."""

import asyncio
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traci import constants as tc

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
from crowdsim.domain.crowdsim_models import MotionSnapshot
from crowdsim.domain.observation_config import DEFERRED_METRIC_IDS, EVACUATION_METRIC_IDS, IMPLEMENTED_METRIC_IDS
from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.sumo_adapter import SumoStepResult
from crowdsim.infrastructure.websocket_server import OverlayServer


ROOT = Path(__file__).resolve().parents[1]
NETWORK = ROOT / "scenarios/shanghai_bund/bund.net.xml"


class FakeAdapter:
    def __init__(self, person):
        self.extra_args = []
        self.closed = False
        self.started = False
        self.min_expected_number = 1
        self.person = person
        self.now = 0.0

    @property
    def diagnostics(self):
        return {"backend": "test_snapshot_adapter", "closed": self.closed}

    def start(self):
        self.started = True
        return SumoStepResult(0, {"a": self.person}, {}, ("a",), ())

    def step(self):
        self.now += 0.5
        return SumoStepResult(self.now, {"a": self.person}, {}, (), ())

    def set_person_speed(self, person_id, speed):
        pass

    def display_edge(self, person_id, edge_id):
        return edge_id

    def close(self):
        self.closed = True


class ObservationRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = ResearchNetwork(str(NETWORK))
        xy = cls.network.net.getNode("monument_m3_ring_junction").getCoord()
        cls.x, cls.y = int(xy[0] / 20) * 20, int(xy[1] / 20) * 20

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.config = self.directory / "test.sumocfg"
        self.config.write_text(f'<configuration><input><net-file value="{NETWORK}"/></input><time><step-length value="0.5"/></time></configuration>')
        ring = [self.network.xy_to_lonlat(x, y) for x, y in (
            (self.x - 20, self.y - 20), (self.x + 20, self.y - 20),
            (self.x + 20, self.y + 20), (self.x - 20, self.y + 20), (self.x - 20, self.y - 20))]
        self.payload = {
            "schema_version": 1, "project": {"title": "观测测试", "description": "仅使用模拟快照"},
            "spatial_scope": {"source": "preset", "location_id": "memorial-tower", "name": "纪念塔", "crs": "EPSG:4326",
                "center": list(self.network.xy_to_lonlat(self.x, self.y)), "boundary": {"type": "Polygon", "coordinates": [[list(item) for item in ring]]}},
            "population": {"total": 1, "distributions": {name: [{"code": "test", "label": "测试", "percent": 100}]
                for name in ("crowd_role", "age_band", "origin", "gender")}},
            "scenario": {"event_category": {"id": "culture"}},
            "observation": {"metric_ids": [*IMPLEMENTED_METRIC_IDS, *DEFERRED_METRIC_IDS], "local_partition_mode": "uniform"},
        }
        self.spec = RequirementSpec.parse(self.payload)
        self.record = {"requirement_id": "req-default", "schema_version": 1, "fingerprint": f"sha256:{self.spec.fingerprint}",
                       "requirement": self.payload, "capabilities": self.spec.capabilities()}
        self.root_patch = patch("crowdsim.core.simulation_runtime.PROJECT_ROOT", self.directory)
        self.root_patch.start()
        self.recorder_patch = patch("crowdsim.core.simulation_runtime.ExperimentRecorder",
            side_effect=lambda *args, **kwargs: ExperimentRecorder(*args, **kwargs, root=self.directory / "runs"))
        self.recorder_patch.start()
        self.runtime = None

    def tearDown(self):
        if self.runtime is not None:
            self.runtime.close()
        self.root_patch.stop()
        self.recorder_patch.stop()
        self.temporary.cleanup()

    def initialize(self, record=True, end=0.5):
        with patch("crowdsim.infrastructure.sumo_adapter.discover_sumo_binary", return_value="never-executed-sumo"), patch.dict("os.environ", {"CROWDSIM_PERF": "0", "CROWDSIM_SYSTEM_PERF": "0"}):
            self.runtime = SimulationRuntime(self.config, requirement_record=self.record if record else None, timeline_end_seconds=end)
            self.runtime.adapter = FakeAdapter(MotionSnapshot("a", 0, self.x, self.y, *self.network.xy_to_lonlat(self.x, self.y),
                1, "monument_m3", "monument_m3_0", 0, 0, 0, tc.STAGE_WALKING))
            self.runtime.initialize()
        return self.runtime

    def tick(self, runtime):
        runtime.start()
        with patch.object(runtime.decision_scheduler, "collect_due", return_value=[]):
            return runtime.tick()

    def read(self, name):
        return json.loads((self.runtime.recorder.directory / name).read_text())

    def test_initial_sample_and_all_seven_metrics_are_persisted(self):
        runtime = self.initialize()
        directory = runtime.recorder.directory
        for name in ("observation_geometry.json", "observation_global.csv", "observation_cells.csv", "observation_boundaries.csv",
                     "state_transitions.jsonl", "observation_samples.jsonl", "observation_result.json"):
            self.assertTrue((directory / name).is_file(), name)
        with (directory / "trajectory.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(1, len(rows))
        self.assertEqual("0", rows[0]["time"])
        self.assertEqual("walking", rows[0]["behavior_state"])
        self.assertEqual("calm", rows[0]["psychological_state"])
        self.assertEqual("0.0", rows[0]["stress"])
        geometry = self.read("observation_geometry.json")
        self.assertEqual(hashlib.sha256(NETWORK.read_bytes()).hexdigest(), geometry["network_sha256"])
        self.assertEqual("selected_polygon_area", geometry["density_area_basis"])
        self.assertEqual(list(self.network.net.getLocationOffset()), geometry["projection"]["net_offset_xy"])
        init = runtime.init_frame()
        self.assertEqual(geometry["area_m2"], init["observation_geometry"]["area_m2"])
        self.assertEqual(1, init["metrics"]["observations"]["global"]["person_count"])
        self.assertEqual("running", self.read("observation_result.json")["status"])

    def test_finished_writes_result_before_disconnect_and_close_does_not_overwrite_it(self):
        runtime = self.initialize()
        self.tick(runtime)
        self.assertEqual(RuntimeState.FINISHED, runtime.state)
        result = self.read("observation_result.json")
        self.assertEqual("complete", result["status"])
        self.assertEqual("timeline_end", result["end_reason"])
        self.assertEqual(2, result["recorded_samples"])
        self.assertTrue(all(result["metrics"][name]["status"] == "recorded" for name in IMPLEMENTED_METRIC_IDS if name not in EVACUATION_METRIC_IDS))
        self.assertEqual("incomplete", result["metrics"]["absolute-evacuation-density"]["status"])
        self.assertEqual("not_started", result["metrics"]["evacuation-time"]["status"])
        self.assertIsNone(result["evacuation"]["completion_time_seconds"])
        self.assertTrue(all(result["metrics"][name]["status"] == "not_implemented" for name in DEFERRED_METRIC_IDS))
        frame = runtime.frame()
        self.assertEqual(runtime.snapshot_id, frame["metrics"]["observations"]["snapshot_id"])
        self.assertEqual("walking", frame["pedestrians"][0]["state"]["behavior_state"])
        runtime.close()
        runtime.close()
        self.assertEqual(result, self.read("observation_result.json"))

    def test_early_close_and_tick_error_preserve_partial_evidence(self):
        runtime = self.initialize(end=10)
        runtime.close()
        self.assertEqual("interrupted", self.read("observation_result.json")["status"])
        self.assertEqual(1, self.read("observation_result.json")["recorded_samples"])
        # A distinct initialized run records the original exception and last good sample.
        runtime = self.initialize(end=10)
        runtime.start()
        with patch.object(runtime, "_prepare_boundary", side_effect=ValueError("forced failure")):
            with self.assertRaisesRegex(ValueError, "forced failure"):
                runtime.tick()
        result = self.read("observation_result.json")
        self.assertEqual("error", result["status"])
        self.assertEqual("runtime_error", result["end_reason"])
        self.assertIn("forced failure", result["error"])
        self.assertEqual(1, result["recorded_samples"])
        self.assertTrue(runtime.adapter.closed)

    def test_partial_multi_file_write_is_excluded_by_snapshot_commit_index(self):
        runtime = self.initialize(end=10)
        writer = runtime.recorder.observation_writer
        append = writer._append_csv

        def fail_cells(name, rows):
            if name == "observation_cells.csv":
                raise OSError("disk full")
            return append(name, rows)

        runtime.start()
        with patch.object(runtime.decision_scheduler, "collect_due", return_value=[]), patch.object(writer, "_append_csv", side_effect=fail_cells):
            with self.assertRaisesRegex(OSError, "disk full"):
                runtime.tick()
        result = self.read("observation_result.json")
        self.assertEqual("error", result["status"])
        self.assertEqual(1, result["recorded_samples"])
        index = [json.loads(line) for line in (writer.directory / "observation_samples.jsonl").read_text().splitlines()]
        self.assertEqual([f"{runtime.run_id}:0"], [row["snapshot_id"] for row in index])
        self.assertEqual(index[-1]["snapshot_id"], result["last_snapshot_id"])

    def test_legacy_run_without_requirement_keeps_original_recording_format(self):
        runtime = self.initialize(record=False)
        self.tick(runtime)
        self.assertIsNone(runtime.observation_collector)
        self.assertFalse((runtime.recorder.directory / "observation_geometry.json").exists())
        self.assertIsNone(runtime.frame()["metrics"]["observations"])
        with (runtime.recorder.directory / "trajectory.csv").open() as handle:
            self.assertNotIn("psychological_state", next(csv.reader(handle)))

    def test_old_capabilities_refresh_without_modifying_requirement_file(self):
        directory = self.directory / "requirements"
        directory.mkdir()
        path = directory / "req-default.json"
        self.record["capabilities"] = {"metrics": {name: "partial" for name in IMPLEMENTED_METRIC_IDS}}
        path.write_text(json.dumps(self.record))
        before = path.read_bytes()
        loaded = RequirementRepository(directory).load("req-default")
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(self.spec.fingerprint, RequirementSpec.parse(loaded["requirement"]).fingerprint)
        self.assertTrue(all(loaded["capabilities"]["metrics"][name] == "supported" for name in IMPLEMENTED_METRIC_IDS))
        self.assertTrue(all(loaded["capabilities"]["metrics"][name] == "not_implemented" for name in DEFERRED_METRIC_IDS))

    def test_random_partition_is_not_silently_reported_as_supported(self):
        self.payload["observation"]["local_partition_mode"] = "random"
        capabilities = RequirementSpec.parse(self.payload).capabilities()
        for name in ("local-density", "local-speed", "boundary-density-difference"):
            self.assertEqual("not_implemented", capabilities["metrics"][name])
        with self.assertRaisesRegex(ValueError, "uniform"):
            self.initialize()
        self.assertFalse(self.runtime.adapter.started)

    def test_last_arrival_finishes_recording_with_zero_density_and_unknown_speed(self):
        runtime = self.initialize(end=10)
        runtime.adapter.min_expected_number = 0
        with patch.object(runtime.adapter, "step", return_value=SumoStepResult(0.5, {}, {}, (), ("a",))):
            self.tick(runtime)
        result = self.read("observation_result.json")
        self.assertEqual("no_expected_entities", result["end_reason"])
        self.assertEqual("complete", result["status"])
        row = runtime.latest_metrics["observation"]["global"]
        self.assertEqual(0, row["density_person_per_m2"])
        self.assertIsNone(row["avg_speed_mps"])
        self.assertTrue(any(row["reason"] == "arrived" for row in runtime.latest_metrics["observation"]["transitions"]))

    def test_paused_policy_is_persisted_immediately_and_resume_preserves_baselines(self):
        runtime = self.initialize(end=2)
        runtime.start()
        runtime.pause()
        baseline = self.read("evacuation_state.json")
        self.assertEqual(0, baseline["event_start_time_seconds"])
        runtime.queue_command("event_decision", {"decision": "police_guidance", "request_id": "policy-first"}, "policy-first")
        receipts = runtime.process_pending_commands()
        self.assertEqual("applied", receipts[0]["status"])
        self.assertEqual(0, receipts[0]["applied_at"])
        self.assertEqual(0, self.read("evacuation_state.json")["strategy_applied_time_seconds"])
        self.assertEqual(1, self.read("observation_result.json")["recorded_samples"])
        runtime.start()
        with patch.object(runtime.decision_scheduler, "collect_due", return_value=[]):
            runtime.tick()
        runtime.pause()
        runtime.apply_policy({"decision": "temporary_diversion", "request_id": "policy-second"})
        evidence = self.read("evacuation_state.json")
        self.assertEqual(0, evidence["event_start_time_seconds"])
        self.assertEqual(0, evidence["strategy_applied_time_seconds"])
        self.assertEqual(.5, evidence["policy_applications"][1]["applied_at"])
        self.assertFalse(evidence["policy_applications"][1]["starts_evacuation"])
        runtime.adapter.min_expected_number = 0
        with patch.object(runtime.adapter, "step", return_value=SumoStepResult(1, {}, {}, (), ("a",))):
            self.tick(runtime)
        result = self.read("observation_result.json")
        self.assertEqual(1, result["metrics"]["evacuation-time"]["value"])
        self.assertAlmostEqual(1 / 1600, result["metrics"]["evacuation-efficiency"]["value"])
        self.assertEqual("complete", result["metrics"]["absolute-evacuation-density"]["status"])
        directory = runtime.recorder.directory
        with (directory / "evacuation_density_differences.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(3 * len(runtime.observation_collector.cells), len(rows))
        occupied = [row for row in rows if float(row["initial_density_person_per_m2"]) > 0]
        self.assertEqual(3, len(occupied))
        self.assertAlmostEqual(1 / 400, float(occupied[0]["absolute_runtime_final_person_per_m2"]))
        self.assertEqual(0, float(occupied[-1]["absolute_runtime_final_person_per_m2"]))
        self.assertAlmostEqual(1 / 400, float(occupied[-1]["absolute_initial_runtime_person_per_m2"]))
        for row in occupied:
            self.assertAlmostEqual(1 / 400, float(row["absolute_initial_final_person_per_m2"]))
        progress = [json.loads(line) for line in (directory / "evacuation_progress.jsonl").read_text().splitlines()]
        self.assertEqual("complete", progress[-1]["status"])
        self.assertEqual("complete", runtime.frame()["metrics"]["observations"]["evacuation"]["status"])

    def test_failed_policy_and_time_limit_do_not_produce_final_evacuation_evidence(self):
        runtime = self.initialize()
        runtime.start()
        runtime.pause()
        runtime.queue_command("event_decision", {"decision": "unsupported-policy"}, "bad-policy")
        self.assertEqual("rejected", runtime.process_pending_commands()[0]["status"])
        self.assertEqual([], self.read("evacuation_state.json")["policy_applications"])
        runtime.apply_policy({"decision": "police_guidance"})
        self.tick(runtime)
        result = self.read("observation_result.json")
        self.assertEqual("incomplete", result["metrics"]["evacuation-time"]["status"])
        self.assertIsNone(result["metrics"]["evacuation-time"]["value"])
        self.assertIsNone(result["metrics"]["evacuation-efficiency"]["value"])
        self.assertIsNone(self.read("evacuation_state.json")["final_density"])
        self.assertFalse((runtime.recorder.directory / "evacuation_density_differences.csv").exists())
        self.assertEqual("incomplete", runtime.frame()["metrics"]["observations"]["evacuation"]["status"])
        progress = [json.loads(line) for line in (runtime.recorder.directory / "evacuation_progress.jsonl").read_text().splitlines()]
        self.assertEqual("incomplete", progress[-1]["status"])
        self.assertEqual("timeline_end", progress[-1]["end_reason"])

    def test_backend_detach_attach_and_policy_retry_keep_one_metric_lifecycle(self):
        runtime = self.initialize(end=2)
        runtime.start()

        class Client:
            def __init__(self):
                self.messages = []

            async def send(self, value):
                self.messages.append(json.loads(value))

        async def round_trip():
            server = OverlayServer(runtime, "unused", 0)
            client = Client()
            server.client = client
            policy = json.dumps({"action": "event_decision", "decision": "police_guidance",
                                 "request_id": "applied-once", "run_id": runtime.run_id})
            runtime.pause()
            await server._handle_message(policy)
            runtime.start()
            await server._detach_runtime()
            self.assertEqual(RuntimeState.PAUSED, runtime.state)
            await server._handle_message(json.dumps({"action": "attach_run", "request_id": "reattach",
                "run_id": runtime.run_id, "requirement_id": runtime.requirement_id,
                "location_id": runtime.location_id, "network_sha256": runtime.network_sha256}))
            self.assertTrue(client.messages[-2]["resumed"])
            await server._handle_message(policy)
            self.assertEqual("applied", client.messages[-1]["status"])
            self.assertEqual(1, len(runtime.observation_collector.evacuation.policy_applications))
            self.assertFalse(runtime.recorder.observation_writer.finalized)
            runtime.start()

        asyncio.run(round_trip())
        evidence = self.read("evacuation_state.json")
        self.assertEqual(0, evidence["event_start_time_seconds"])
        self.assertEqual(0, evidence["strategy_applied_time_seconds"])
        self.assertEqual(1, evidence["policy_application_count"])

    def test_failed_final_snapshot_write_does_not_publish_completed_metrics(self):
        runtime = self.initialize(end=2)
        runtime.start()
        runtime.apply_policy({"decision": "police_guidance"})
        writer = runtime.recorder.observation_writer
        append = writer._append_csv

        def fail_final_cells(name, rows):
            if name == "observation_cells.csv":
                raise OSError("forced cell write failure")
            return append(name, rows)

        with patch.object(runtime.adapter, "step", return_value=SumoStepResult(.5, {}, {}, (), ("a",))), \
                patch.object(runtime.decision_scheduler, "collect_due", return_value=[]), \
                patch.object(writer, "_append_csv", side_effect=fail_final_cells):
            with self.assertRaisesRegex(OSError, "forced cell write failure"):
                runtime.tick()
        result = self.read("observation_result.json")
        self.assertEqual("error", result["status"])
        self.assertEqual("uncommitted_completion", result["evacuation"]["status"])
        self.assertFalse(result["evacuation"]["completion_evidence_committed"])
        self.assertIsNone(result["evacuation"]["completion_time_seconds"])
        self.assertIsNone(result["metrics"]["evacuation-time"]["value"])
        self.assertIsNone(result["metrics"]["evacuation-efficiency"]["value"])
        self.assertIsNone(result["metrics"]["absolute-evacuation-density"]["final_comparisons_file"])
        self.assertFalse((writer.directory / "evacuation_density_differences.csv").exists())

    def test_failed_result_publication_can_retry_without_repeating_samples(self):
        runtime = self.initialize()
        writer = runtime.recorder.observation_writer
        before = (writer.directory / "observation_result.json").read_bytes()
        with patch.object(writer, "_atomic_json", side_effect=OSError("result failed")):
            with self.assertRaisesRegex(OSError, "result failed"):
                writer.finalize("complete", "test_finish")
        self.assertFalse(writer.finalized)
        self.assertEqual(before, (writer.directory / "observation_result.json").read_bytes())
        writer.finalize("complete", "test_finish")
        self.assertEqual(1, self.read("observation_result.json")["recorded_samples"])
        writer.record(runtime.latest_metrics["observation"])
        self.assertEqual(1, writer.recorded_samples)

    def test_active_run_keeps_immutable_scope_until_reset(self):
        runtime = self.initialize()
        original_area = runtime.observation_collector.scope.area
        runtime.configure_requirement(self.record)
        altered = json.loads(json.dumps(self.record))
        altered["requirement"]["spatial_scope"]["boundary"]["coordinates"][0][0][0] += 0.001
        with self.assertRaisesRegex(ValueError, "reset"):
            runtime.configure_requirement(altered)
        self.assertEqual(original_area, runtime.observation_collector.scope.area)


if __name__ == "__main__":
    unittest.main()
