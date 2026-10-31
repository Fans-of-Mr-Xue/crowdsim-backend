"""Post-only intake contracts: confirmations, source evidence and sample bodies."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from dialogue_service.post_agent import validate_reply
from dialogue_service.post_research import collect_sources, _search
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.schemas.datasets import validate_dataset
from postanalysis_api.schemas.review_intake import validate_review_intake


EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples"


class PostReviewIntakeContractTests(unittest.TestCase):
    def test_documented_dataset_and_plan_are_accepted(self):
        dataset = json.loads((EXAMPLES / "post_review_dataset.json").read_text(encoding="utf-8"))
        self.assertIsNone(validate_dataset(dataset)["observations"][0]["pressureProxy"])
        plan = json.loads((EXAMPLES / "post_review_plan.json").read_text(encoding="utf-8"))["plan"]
        validate_review_intake(plan)

    def test_incomplete_or_changed_confirmations_cannot_be_submitted(self):
        plan = json.loads((EXAMPLES / "post_review_plan.json").read_text(encoding="utf-8"))["plan"]
        for mutation in (
            lambda p: p["reviewIntake"].update(baselineConfirmed=False),
            lambda p: p["reviewIntake"].update(goalConfirmed=False),
            lambda p: p["reviewIntake"]["confirmedStages"].update(mechanism=False),
            lambda p: p["experimentSchemeDesign"].update(intervention=[]),
            lambda p: p["experimentSchemeDesign"]["observation"][0].update(hypothesis=""),
            lambda p: p["counterfactualSettings"].update(repeatCount=0),
        ):
            changed = deepcopy(plan)
            mutation(changed)
            with self.assertRaises(ApiError):
                validate_review_intake(changed)

    def test_baseline_review_cannot_rewrite_dataset_or_measurements(self):
        validate_reply("baseline_review", {"assistant_message": "请核对", "baseline_updates": {"location": "用户指定区域"}}, {})
        for key in ("datasetId", "rows", "synthetic", "reconstructionConfirmed"):
            with self.assertRaises(ValueError):
                validate_reply("baseline_review", {"assistant_message": "已更新", "baseline_updates": {key: "invalid"}}, {})

    def test_goal_proposal_is_advice_and_cannot_confirm_the_user_goal(self):
        validate_reply("goal_review", {"assistant_message": "请补充范围", "proposed_goal": "比较风险", "missing_fields": ["时间窗"]}, {})
        with self.assertRaises(ValueError):
            validate_reply("goal_review", {"assistant_message": "已确认", "goalConfirmed": True}, {})

    def test_web_sources_must_have_been_collected_and_cannot_provide_model_generated_rows(self):
        context = {"collectedEvidence": {"sources": [{"url": "https://example.org/verified"}]}}
        reply = {"assistant_message": "资料待确认", "rows": [], "sources": [{"url": "https://example.org/verified"}]}
        validate_reply("historical_event", reply, context)
        with self.assertRaises(ValueError):
            validate_reply("historical_event", reply, {})
        with self.assertRaises(ValueError):
            validate_reply("historical_event", {**reply, "rows": [{"time": 0, "crowd": 100}]}, context)

    def test_current_stage_cannot_modify_other_experiment_types(self):
        context = {"designStage": "observation", "experimentSchemes": {"mechanism": [{"id": "MEC-01"}]}}
        with self.assertRaises(ValueError):
            validate_reply("design", {"assistant_message": "修改机制组", "experiment_updates": [{"id": "MEC-01", "scheme_type": "mechanism", "change": "不同条件"}]}, context)

    def test_search_without_configuration_reports_missing_sources(self):
        with patch.dict("os.environ", {"CROWDSIM_POST_SEARCH_API_KEY": ""}):
            result = collect_sources("示例事件")
        self.assertEqual("needs_sources", result["status"])
        self.assertEqual([], result["sources"])
        self.assertTrue(result["notice"])

    def test_configured_search_uses_header_auth_and_limits_results(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return json.dumps({"results": [{"url": f"https://example.org/{i}"} for i in range(8)]}).encode()

        with patch.dict("os.environ", {"CROWDSIM_POST_SEARCH_API_KEY": "test-only", "CROWDSIM_POST_SEARCH_URL": "https://example.org/search"}):
            with patch("dialogue_service.post_research.urlopen", return_value=Response()) as fetch:
                urls, notice = _search("示例事件")
        self.assertEqual(3, len(urls))
        self.assertEqual("", notice)
        request = fetch.call_args.args[0]
        self.assertEqual("Bearer test-only", request.get_header("Authorization"))
        self.assertNotIn("api_key", json.loads(request.data))


if __name__ == "__main__":
    unittest.main()
