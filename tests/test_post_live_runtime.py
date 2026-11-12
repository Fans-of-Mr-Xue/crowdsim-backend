"""Post-only display frames follow real run clocks without advancing completion."""
from copy import deepcopy
import tempfile
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from postanalysis_api.api.common import Request
from postanalysis_api.api.experiments import handle as experiment_route
from postanalysis_api.api.simulations import handle as simulation_route
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.services.application import PostAnalysisApp
from postanalysis_api.workers.live_frames import capture_frame, geographic_trajectory
from postanalysis_api.workers.sumo_batch import SumoBatchExecutor


class MemoryRepository:
    def __init__(self):
        self.records, self.archives = {}, []

    def create(self, kind, user, workspace, data):
        identifier = f"{kind}_{len(self.records)}"
        self.records[identifier] = {"id": identifier, **deepcopy(data)}
        return deepcopy(self.records[identifier])

    def update(self, kind, identifier, user, workspace, data):
        self.records[identifier].update(deepcopy(data))
        return deepcopy(self.records[identifier])

    def begin_archive(self, identifier, kind, user, workspace, data):
        return {"runId": identifier}

    def finish_archive(self, archive, status, data):
        self.archives.append((archive["runId"], status))


class PostLiveRuntimeTests(unittest.TestCase):
    def executor(self, repository=None, folder="."):
        executor = SumoBatchExecutor.__new__(SumoBatchExecutor)
        from collections import OrderedDict
        executor.repo = repository or MemoryRepository()
        executor.data_dir, executor.binary = Path(folder), "sumo"
        executor._lock, executor._slot = threading.RLock(), threading.Semaphore(1)
        executor._runtime, executor._jobs, executor._threads = OrderedDict(), {}, {}
        return executor

    def test_live_frame_projects_real_xy_and_explicitly_bounds_display_people(self):
        people = SimpleNamespace(getIDList=lambda: ["p2", "p1", "p3"], getPosition=lambda identifier: (10, 20),
                                 getSpeed=lambda identifier: 0.4, getAngle=lambda identifier: 90,
                                 getRoadID=lambda identifier: "edge")
        projection = SimpleNamespace(convertXY2LonLat=lambda x, y: (121 + x / 10000, 31 + y / 10000))
        with patch("postanalysis_api.workers.live_frames.network", return_value=projection), \
                patch("postanalysis_api.workers.live_frames.MAX_LIVE_PERSONS", 2):
            frame = capture_frame(SimpleNamespace(person=people), 15)
        self.assertEqual(["p1", "p2"], [person["id"] for person in frame["persons"]])
        self.assertEqual(3, frame["totalPersons"])
        self.assertTrue(frame["truncated"])
        self.assertEqual((10, 20, 121.001, 31.002), tuple(frame["persons"][0][key] for key in ("x", "y", "lng", "lat")))
        self.assertEqual("sumo-traci", frame["source"])

    def test_archived_projection_enriches_response_without_mutating_raw_archive(self):
        raw = {"items": [{"frames": [{"timeSeconds": 5, "persons": [{"id": "p1", "x": 1, "y": 2, "speed": 0}]}]}]}
        original = deepcopy(raw)
        with patch("postanalysis_api.workers.live_frames.network", return_value=SimpleNamespace(convertXY2LonLat=lambda x, y: (121.5, 31.2))):
            projected = geographic_trajectory(raw)
        self.assertEqual(original, raw)
        self.assertEqual(121.5, projected["items"][0]["frames"][0]["persons"][0]["lng"])
        self.assertEqual("WGS84", projected["coordinateSystem"])

    def test_runtime_keeps_counts_separate_and_scopes_access_before_memory_lookup(self):
        executor = self.executor()
        executor._publish("exp_1", status="running", activeRun={"runId": "run_1", "phase": "running", "progress": 0.5},
                          frame={"timeSeconds": 5, "persons": []})
        executor._publish("exp_2", status="running", frame={"timeSeconds": 3, "persons": [{"id": "other"}]})
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        app.executor = executor
        app.repo = SimpleNamespace(get=Mock(return_value={"status": "cancelling", "progress": {"baselineCompleted": 0}}))
        result = app.runtime("experiments", "exp_1", "user", "workspace")
        self.assertEqual("cancelling", result["status"])
        self.assertEqual(0, result["progress"]["baselineCompleted"])
        self.assertEqual(0.5, result["activeRun"]["progress"])
        self.assertEqual([], result["frame"]["persons"])
        result["frame"]["persons"].append({"id": "mutated"})
        self.assertEqual([], executor.runtime("exp_1")["frame"]["persons"])
        app.repo.get.side_effect = ApiError("RESOURCE_FORBIDDEN", "forbidden", 403)
        with patch.object(executor, "runtime") as lookup:
            with self.assertRaises(ApiError):
                app.runtime("experiments", "exp_1", "other-user", "workspace")
            lookup.assert_not_called()

    def test_restarted_worker_reports_unavailable_instead_of_fake_live_positions(self):
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        app.executor = self.executor()
        app.repo = SimpleNamespace(get=lambda *args: {"status": "interrupted", "progress": {"completed": 0},
                                  "activeRun": {"runId": "run_1", "phase": "running", "stepSeconds": 100}})
        result = app.runtime("simulations", "sim_1", "user", "workspace")
        self.assertFalse(result["liveAvailable"])
        self.assertIsNone(result["frame"])
        self.assertEqual("worker_not_active", result["availableReason"])
        self.assertEqual("interrupted", result["activeRun"]["phase"])

    def test_worker_completion_count_is_retained_while_task_is_cancelling(self):
        task = {"status": "cancelling", "progress": {"planned": 1, "completed": 0, "failed": 0}}
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        app.repo = SimpleNamespace(get=Mock(return_value=task), transition_task=Mock(), event=Mock())
        app.record_worker_event("simulations", "sim_1", "user", "workspace", {
            "status": "running", "progress": {"completed": 1},
            "activeRun": {"phase": "completed", "runId": "run_1"}})
        changes = app.repo.transition_task.call_args.args[-1]
        self.assertEqual("cancelling", changes["status"])
        self.assertEqual(1, changes["progress"]["completed"])
        self.assertEqual("completed", changes["activeRun"]["phase"])

    def test_cancel_winning_worker_compare_and_set_does_not_fail_the_run(self):
        task = {"status": "running", "progress": {"planned": 1, "completed": 0, "failed": 0}}
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        app.repo = SimpleNamespace(get=Mock(side_effect=[task, {**task, "status": "cancelling"}]),
                                   transition_task=Mock(side_effect=[ApiError("INVALID_TASK_STATE", "changed", 409), {}]),
                                   event=Mock())
        app.record_worker_event("simulations", "sim_1", "user", "workspace", {"status": "running", "progress": {"completed": 1}})
        self.assertEqual(2, app.repo.transition_task.call_count)
        changes = app.repo.transition_task.call_args.args[-1]
        self.assertEqual("cancelling", changes["status"])
        self.assertEqual(1, changes["progress"]["completed"])

    def test_terminal_cache_is_bounded_and_running_jobs_are_not_evicted(self):
        executor = self.executor()
        executor._publish("running", status="running", frame={"timeSeconds": 1})
        for index in range(70):
            executor._publish(f"ended_{index}", status="completed", frame={"timeSeconds": index})
        self.assertEqual(65, len(executor._runtime))
        self.assertIsNotNone(executor.runtime("running"))
        self.assertIsNone(executor.runtime("ended_0"))
        self.assertIsNotNone(executor.runtime("ended_69"))

    def test_both_runtime_routes_use_the_scoped_application_method(self):
        app = SimpleNamespace(runtime=Mock(return_value={"status": "running"}))
        headers = {"X-CrowdSim-User": "user", "X-CrowdSim-Workspace": "workspace"}
        for route, kind, identifier in ((experiment_route, "experiments", "exp_1"), (simulation_route, "simulations", "sim_1")):
            response = route(Request("GET", f"/{kind}/{identifier}/runtime", {}, {}, headers, app))
            self.assertEqual(200, response[0])
            app.runtime.assert_called_with(kind, identifier, "user", "workspace")

    def batch(self, fail=None, fail_plan="F-00", completed_callback_error=False):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        repo = MemoryRepository()
        task = {"id": "exp_1", "ownerId": "user", "workspaceId": "workspace", "datasetId": "ds_1", "datasetVersion": "v1",
                "scenario": {"durationSeconds": 10}, "seedStart": 7, "groups": [
                    {"id": "OBS-01", "experimentType": "observation", "repeatCount": 1, "interventions": []}],
                "progress": {"baselinePlanned": 1, "baselineCompleted": 0, "interventionPlanned": 1,
                             "interventionCompleted": 0, "failed": 0,
                             "groups": [{"groupId": "OBS-01", "planned": 1, "completed": 0, "failed": 0}]}}
        repo.records[task["id"]] = deepcopy(task)
        executor, events, observed = self.executor(repo, folder.name), [], []
        executor._publish(task["id"], status="queued")

        def execute(binary, routes, directory, arguments, manifest, duration, cancelled, *, on_progress):
            for second in (1, 5, 10):
                on_progress({"stepSeconds": second, "frame": {"timeSeconds": second, "persons": [], "totalPersons": 0}})
                observed.append(executor.runtime(task["id"]))
                if fail and executor.runtime(task["id"])["activeRun"]["planId"] == fail_plan:
                    raise fail()
            (directory / "tripinfo.xml").write_text("<tripinfos/>", encoding="utf-8")
            return []

        def compile_routes(dataset, seed, actions, duration, path):
            path.write_text("<routes/>", encoding="utf-8")
            return {"cohortCount": 0, "cohorts": [], "controls": {}, "assumptions": []}

        def on_event(event):
            events.append(deepcopy(event))
            if completed_callback_error and event.get('activeRun', {}).get('phase') == 'completed':
                raise ApiError('INVALID_TASK_STATE', 'late lifecycle write failed', 409)

        with patch("postanalysis_api.workers.sumo_batch.compile_routes", side_effect=compile_routes), \
                patch("postanalysis_api.workers.sumo_batch.execute", side_effect=execute), \
                patch("postanalysis_api.workers.sumo_batch.measure", return_value={"metrics": {}, "measurement": {}, "regionSamples": {}}), \
                patch("postanalysis_api.workers.sumo_batch.summarize", return_value={}):
            executor._run(task, {"observations": []}, on_event, threading.Event())
        return executor, events, observed, repo

    def test_batch_frames_expose_real_run_index_without_persisting_each_frame(self):
        executor, events, observed, repo = self.batch()
        self.assertEqual([1, 1, 1, 2, 2, 2], [state["activeRun"]["runIndex"] for state in observed])
        self.assertEqual([0.1, 0.5, 1] * 2, [state["activeRun"]["progress"] for state in observed])
        self.assertEqual(["F-00"] * 3 + ["OBS-01"] * 3, [state["activeRun"]["planId"] for state in observed])
        self.assertEqual(6, len([event for event in events if "activeRun" in event]))
        self.assertTrue(all("frame" not in event for event in events))
        self.assertEqual(1, events[-1]["progress"]["baselineCompleted"])
        self.assertEqual(1, events[-1]["progress"]["interventionCompleted"])
        self.assertEqual("completed", executor.runtime("exp_1")["status"])

    def test_cancellation_preserves_partial_clock_and_never_counts_run_complete(self):
        executor, events, observed, repo = self.batch(InterruptedError)
        state = executor.runtime("exp_1")
        self.assertEqual("cancelled", state["status"])
        self.assertEqual("cancelled", state["activeRun"]["phase"])
        self.assertEqual(1, state["activeRun"]["stepSeconds"])
        self.assertEqual(1, state["frame"]["timeSeconds"])
        self.assertFalse(any(event.get("progress", {}).get("baselineCompleted") for event in events))
        self.assertEqual("cancelled", events[-1]["status"])
        self.assertIn(("runs_1", "cancelled"), repo.archives)

    def test_failed_run_reports_failure_count_without_incrementing_success(self):
        executor, events, observed, repo = self.batch(RuntimeError)
        self.assertEqual("failed", events[-1]["status"])
        self.assertEqual(1, events[-1]["progress"]["failed"])
        self.assertEqual(0, events[-1]["progress"]["baselineCompleted"])
        self.assertEqual("failed", executor.runtime("exp_1")["activeRun"]["phase"])

    def test_failed_intervention_preserves_completed_baseline_and_marks_its_own_group(self):
        executor, events, observed, repo = self.batch(RuntimeError, fail_plan="OBS-01")
        progress = events[-1]["progress"]
        self.assertEqual(1, progress["baselineCompleted"])
        self.assertEqual(0, progress["interventionCompleted"])
        self.assertEqual(1, progress["failed"])
        self.assertEqual({"groupId": "OBS-01", "planned": 1, "completed": 0, "failed": 1}, progress["groups"][0])
        self.assertEqual("OBS-01", executor.runtime("exp_1")["activeRun"]["planId"])

    def test_a_late_callback_error_does_not_overwrite_an_already_archived_completed_run(self):
        _executor, events, _observed, repo = self.batch(completed_callback_error=True)
        self.assertEqual('failed', events[-1]['status'])
        self.assertEqual('completed', repo.records['runs_1']['status'])
        self.assertNotIn(('runs_1', 'failed'), repo.archives)


if __name__ == "__main__":
    unittest.main()
