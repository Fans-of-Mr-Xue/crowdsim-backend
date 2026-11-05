from copy import deepcopy
from types import SimpleNamespace
import unittest

from postanalysis_api.api.common import Request
from postanalysis_api.api.datasets import handle
from postanalysis_api.schemas.datasets import validate_dataset
from postanalysis_api.services.distribution_targets import distribution_targets
from postanalysis_api.services.execution_template import execution_readiness


class DistributionTargetTests(unittest.TestCase):
    def dataset(self):
        return validate_dataset({"name": "路口分布", "sourceKind": "synthetic_reference", "sourceNote": "事中分布用于事后重构",
            "observations": [{"timeSeconds": t, "regionId": region, "population": n}
                for t, values in ((.5, (80, 0)), (1.0, (75, 5))) for region, n in zip(("J30", "J34"), values)]})

    def test_counts_alone_are_valid_reconstruction_targets_without_historical_configuration(self):
        data = self.dataset()
        result = distribution_targets(data)
        self.assertTrue(result["inputReady"])
        self.assertEqual(2, result["stepCount"])
        self.assertEqual(["J30", "J34"], result["regionIds"])
        self.assertIsNone(data["observations"][0]["density"])
        self.assertEqual(0, data["observations"][1]["population"])
        data["metadata"] = {"controlCapabilities": [{"actionType": "invented_source_action"}]}
        readiness = execution_readiness(data)
        self.assertEqual("FACTUAL_RECONSTRUCTION_REQUIRED", readiness["code"])
        self.assertTrue(readiness["reconstruction"]["inputReady"])
        self.assertEqual("/api/v1/post/capabilities", result["actionCapabilitiesSource"])
        self.assertNotIn("invented_source_action", str(readiness))
        self.assertNotIn("manifest", str(readiness["missing"]))

    def test_missing_spatial_counts_are_not_fabricated_from_globals_or_unrecorded_zeroes(self):
        data = self.dataset()
        data["observations"].pop()
        self.assertFalse(distribution_targets(data)["inputReady"])
        data = self.dataset()
        for row in data["observations"]:
            row["regionId"] = "global"
        result = distribution_targets(data)
        self.assertFalse(result["inputReady"])
        self.assertIn("全局", result["missing"][0])

    def test_reconstruction_endpoint_keeps_workspace_scope_and_does_not_run_or_mutate(self):
        data = self.dataset(); before = deepcopy(data); calls = []
        def get(*args):
            calls.append(args); return data
        app = SimpleNamespace(repo=SimpleNamespace(get=get))
        request = Request("GET", "/datasets/ds-one/reconstruction", {}, {}, {"X-CrowdSim-User": "u", "X-CrowdSim-Workspace": "post-c1"}, app)
        status, result, _ = handle(request)
        self.assertEqual(200, status)
        self.assertTrue(result["inputReady"])
        self.assertEqual([("datasets", "ds-one", "u", "post-c1")], calls)
        self.assertEqual(before, data)
