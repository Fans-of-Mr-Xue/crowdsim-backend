"""Post-only dataset imports preserve actual during sampling and provenance."""
from copy import deepcopy
import unittest
from types import SimpleNamespace
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.schemas.datasets import validate_dataset
from postanalysis_api.services.application import PostAnalysisApp


class DuringImportDatasetTests(unittest.TestCase):
    def payload(self):
        return {"name": "事中完成轮次导入", "sourceKind": "synthetic_reference", "sourceNote": "复制原始观测",
            "observations": [{"timeSeconds": time, "regionId": "global", "population": population, "density": density, "meanSpeed": speed, "pressureProxy": None}
                for time, population, density, speed in ((0, 10, 2, .8), (5.5, 0, 0, 0))],
            "metadata": {"sourceBinding": {"sourcePhase": "during", "conversationId": "during-source", "experimentId": "e1", "runId": "r1", "executionVersion": 4, "modelFingerprint": "model-v1"},
                "sourceSeries": [{"timeSeconds": 5.5, "personCount": 0}], "missingFields": ["pressure"]}}

    def test_fractional_last_step_zero_and_null_survive_validation(self):
        data = validate_dataset(self.payload())
        self.assertEqual(5.5, data["observations"][-1]["timeSeconds"])
        self.assertEqual(0, data["observations"][-1]["population"])
        self.assertEqual(0, data["observations"][-1]["meanSpeed"])
        self.assertIsNone(data["observations"][-1]["pressureProxy"])
        self.assertEqual("model-v1", data["metadata"]["sourceBinding"]["modelFingerprint"])

    def test_uploaded_observations_also_preserve_fractional_sampling(self):
        body = self.payload(); body.pop("metadata")
        self.assertEqual(5.5, validate_dataset(body)["observations"][-1]["timeSeconds"])

    def test_spatial_metrics_preserve_null_zero_and_snapshot_identifiers(self):
        body = self.payload()
        body["observations"][0].update(localDensity=3.2, localMeanSpeed=0, boundaryDensityDifference=None, sourceSnapshotId="run-one:0")
        row = validate_dataset(body)["observations"][0]
        self.assertEqual((3.2, 0, None, "run-one:0"), tuple(row[key] for key in ("localDensity", "localMeanSpeed", "boundaryDensityDifference", "sourceSnapshotId")))
        for key in ("localDensity", "localMeanSpeed", "boundaryDensityDifference"):
            body["observations"][0][key] = -1
            with self.assertRaises(ApiError):
                validate_dataset(body)
            body["observations"][0][key] = None

    def test_imported_dataset_and_timeline_read_back_exact_samples_in_the_post_workspace(self):
        records = {}
        def create(kind, user, workspace, data):
            document = {"id": f"dataset-{len(records) + 1}", **deepcopy(data)}
            records[(user, workspace, document["id"])] = document
            return deepcopy(document)
        def get(kind, id_, user, workspace):
            if (user, workspace, id_) not in records:
                raise ApiError("NOT_FOUND", "数据集不属于当前工作区", 404)
            return deepcopy(records[(user, workspace, id_)])
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        app.repo = SimpleNamespace(create=create, get=get, event=lambda *args, **kwargs: None)
        dataset = app.create_dataset("user", "post-target", self.payload())
        timeline = app.timeline(dataset["id"], "user", "post-target", {})
        self.assertEqual(2, timeline["total"])
        self.assertEqual(5.5, timeline["items"][-1]["timeSeconds"])
        self.assertEqual(dataset["version"], timeline["datasetVersion"])
        self.assertEqual("r1", app._dataset(dataset["id"], "user", "post-target")["metadata"]["sourceBinding"]["runId"])
        with self.assertRaises(ApiError):
            app.timeline(dataset["id"], "user", "another-post-workspace", {})
        changed = self.payload(); changed["metadata"]["sourceBinding"]["executionVersion"] += 1
        self.assertNotEqual(dataset["version"], app.create_dataset("user", "post-target", changed)["version"])

    def test_duplicate_nonfinite_and_negative_sample_times_are_rejected(self):
        for time in (0, float("nan"), float("inf"), -0.5, True):
            body = deepcopy(self.payload()); body["observations"][-1]["timeSeconds"] = time
            with self.subTest(time=time), self.assertRaises(ApiError):
                validate_dataset(body)


if __name__ == "__main__":
    unittest.main()
