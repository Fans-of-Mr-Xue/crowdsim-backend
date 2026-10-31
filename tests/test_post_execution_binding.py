"""Reviewed group binding and numerical isolation are enforced before execution."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from postanalysis_api.schemas.common import ApiError
from postanalysis_api.services.application import PostAnalysisApp
from postanalysis_api.services.execution_template import execution_template
from postanalysis_api.repositories.mongodb import MongoRepository
from postanalysis_api.schemas.review_intake import validate_review_intake

ROOT = Path(__file__).resolve().parents[1]


class PostExecutionBindingTests(unittest.TestCase):
    def test_trajectory_compression_round_trips_all_coordinates_and_rejects_corruption(self):
        from postanalysis_api.repositories.trajectory_codec import encode, decode
        frames = [{"timeSeconds": second, "persons": [{"id": "p1", "x": 12.34, "y": 56.78, "speed": 0}]} for second in (0, 5, 10)]
        self.assertEqual(frames, decode(encode(frames))["frames"])
        corrupted = encode(frames)
        corrupted["compressedFrames"] = corrupted["compressedFrames"][:-2]
        with self.assertRaises(ApiError):
            decode(corrupted)

    def fixture(self):
        dataset = json.loads((ROOT / "docs/examples/shanghai_memorial_post_dataset.json").read_text(encoding="utf-8"))
        dataset.update(id="ds_1", version="a" * 64)
        body = json.loads((ROOT / "docs/examples/post_review_plan.json").read_text(encoding="utf-8"))
        body.update(id="plan_1", datasetId=dataset["id"], datasetVersion=dataset["version"])
        body["plan"]["factualBaseline"].update(datasetId=dataset["id"], datasetVersion=dataset["version"])
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        app.repo = SimpleNamespace(get=lambda kind, *args: dataset if kind == "datasets" else body,
                                   create=lambda kind, user, workspace, data: {"id": "exp_1", **data}, event=lambda *args, **kwargs: None)
        request = execution_template(dataset, body["plan"])
        request["reviewPlanId"] = "plan_1"
        return app, request

    def test_complete_template_preserves_all_reviewed_groups(self):
        app, request = self.fixture()
        task = app.create_experiment("user", "workspace", request)
        self.assertEqual(5, len(task["groups"]))
        self.assertEqual({"OBS-01", "INT-01", "MEC-01"}, {group["sourceGroupId"] for group in task["groups"]})
        self.assertEqual(6, task["progress"]["baselinePlanned"])
        self.assertEqual(64, len(task["reviewPlanVersion"]))
        self.assertEqual("post-example-001", task["reviewSnapshot"]["conversationId"])

    def test_editable_run_and_analysis_settings_survive_template_and_frozen_task(self):
        app, _ = self.fixture()
        dataset, reviewed = app.repo.get("datasets"), app.repo.get("review_plans")
        settings = {"seedStart": 1234, "parallelism": 1, "metricIds": ["peak_density", "mean_speed"], "pcFeatures": [
            {"id": "density_A", "metricId": "peak_density", "regionId": "A", "windowStartSeconds": 100, "windowEndSeconds": 200, "aggregation": "peak"},
            {"id": "speed_B", "metricId": "mean_speed", "regionId": "B", "windowStartSeconds": 200, "windowEndSeconds": 500, "aggregation": "mean"}]}
        reviewed["plan"]["executionSettings"] = deepcopy(settings)
        validate_review_intake(reviewed["plan"])
        request = execution_template(dataset, reviewed["plan"])
        request["reviewPlanId"] = reviewed["id"]
        task = app.create_experiment("user", "workspace", request)
        for key, value in settings.items():
            self.assertEqual(value, task[key])
        self.assertEqual(settings, task["reviewSnapshot"]["executionSettings"])
        self.assertTrue(all(group["manipulatedFeatureId"] == "density_A" for group in task["groups"]))
        tampered = deepcopy(request)
        tampered["seedStart"] += 1
        with self.assertRaisesRegex(ApiError, "工作台快照不一致"):
            app.create_experiment("user", "workspace", tampered)
        reviewed["plan"]["executionSettings"]["seedStart"] = 999
        self.assertEqual(1234, task["reviewSnapshot"]["executionSettings"]["seedStart"])

    def test_invalid_settings_are_rejected_before_review_snapshot_is_accepted(self):
        app, _ = self.fixture()
        reviewed = app.repo.get("review_plans")
        dataset = app.repo.get("datasets")
        defaults = execution_template(dataset)
        settings = {key: defaults[key] for key in ("seedStart", "parallelism", "metricIds", "pcFeatures")}
        for mutate in (
            lambda value: value.update(parallelism=2),
            lambda value: value.update(metricIds=["mean_speed"]),
            lambda value: value["pcFeatures"][0].update(windowEndSeconds=0),
            lambda value: value["pcFeatures"][0].update(regionId="unknown"),
            lambda value: value.update(seedStart=2147483647),
        ):
            with self.subTest(mutate=mutate):
                reviewed["plan"]["executionSettings"] = deepcopy(settings)
                mutate(reviewed["plan"]["executionSettings"])
                with self.assertRaises(ApiError):
                    validate_review_intake(reviewed["plan"])

    def test_stale_review_snapshot_cannot_be_silently_executed(self):
        app, request = self.fixture()
        request["reviewPlanVersion"] = "0" * 64
        with self.assertRaisesRegex(ApiError, "方案发生变化"):
            app.create_experiment("user", "workspace", request)

    def test_missing_reviewed_group_and_nonisolated_ablation_are_rejected(self):
        app, request = self.fixture()
        changed = deepcopy(request)
        changed["groups"].pop()
        with self.assertRaisesRegex(ApiError, "全部设计组"):
            app.create_experiment("user", "workspace", changed)
        changed = deepcopy(request)
        changed["groups"][-1]["interventions"][0]["atSeconds"] += 60
        changed["groups"][-1]["interventions"][1]["atSeconds"] += 60
        with self.assertRaisesRegex(ApiError, "消融"):
            app.create_experiment("user", "workspace", changed)

    def test_native_mongo_id_is_not_leaked_in_json_response(self):
        repo = MongoRepository.__new__(MongoRepository)
        collection = Mock()
        collection.insert_one.side_effect = lambda document: document.update(_id=object())
        repo._collection = lambda kind: collection
        result = repo.create("runs", "user", "workspace", {"status": "running"})
        self.assertNotIn("_id", result)
        json.dumps(result, allow_nan=False)

    def test_compiled_inputs_are_archived_with_owner_and_run_binding(self):
        repo = MongoRepository.__new__(MongoRepository)
        repo.files, repo.db = Mock(), {"post_detail_test": Mock()}
        repo.files.upload_from_stream.side_effect = ["manifest-id", "route-id"]
        archive = {"runId": "run_1", "ownerId": "user", "workspaceId": "workspace", "collections": {"detail": "post_detail_test"}}
        repo.archive_execution_inputs(archive, b'{}', b'<routes/>', ['sumo', '-c', 'post.sumocfg'])
        for call in repo.files.upload_from_stream.call_args_list:
            self.assertEqual("run_1", call.kwargs["metadata"]["runId"])
            self.assertEqual("workspace", call.kwargs["metadata"]["workspaceId"])
        record = repo.db["post_detail_test"].insert_one.call_args.args[0]
        self.assertEqual("manifest-id", record["compiledInput"]["manifestFileId"])
        self.assertEqual(64, len(record["compiledInput"]["routeSha256"]))


if __name__ == "__main__":
    unittest.main()
