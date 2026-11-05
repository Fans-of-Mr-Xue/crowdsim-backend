"""Unified pool survives review storage, execution binding and analysis filtering."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from dialogue_service.post_agent import respond, validate_reply, UNIFIED_DESIGN_CONTRACT
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.schemas.review_intake import validate_review_intake
from postanalysis_api.services.application import PostAnalysisApp
from postanalysis_api.services.execution_template import execution_template
from postanalysis_api.workers.analysis import summarize

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    dataset = json.loads((ROOT / "docs/examples/shanghai_memorial_post_dataset.json").read_text(encoding="utf-8"))
    dataset.update(id="ds-1", version="a" * 64)
    settings = {key: value for key, value in execution_template(dataset).items() if key in {"seedStart", "parallelism", "metricIds", "pcFeatures"}}
    group = lambda id_, method, start, compliance, reference: {"id": id_, "name": id_, "hypothesis": "风险暴露可能下降", "method": method,
        "factorSymbols": ["T_start", "Compliance", "Delay"], "primaryMetricId": "risk_exposure", "expectedDirection": "decrease", "decisionRule": "比较配对差值及区间",
        "execution": {"startSeconds": start, "compliance": compliance, "delaySeconds": 0, "referenceDesignId": reference}}
    groups = [group("CF-01", "paired", 13800, .7, "F-00"), group("CF-02", "ofat", 13800, .7, "CF-01"),
              group("CF-03", "ofat", 14400, .7, "CF-01"), group("CF-04", "ablation", 13800, 0, "CF-01")]
    definition = lambda symbol: {"name": symbol, "symbol": symbol, "range": "test range", "calculation": "test definition"}
    plan = {"id": "draft-1", "conversationId": "c1", "label": "统一实验池", "userIntent": "比较处置时点和遵从模块", "factualBaseline": {"datasetId": dataset["id"], "datasetVersion": dataset["version"], "reconstructionConfirmed": True},
        "reviewIntake": {"schemaVersion": 2, "baselineConfirmed": True, "goalConfirmed": True, "societyConfirmed": True, "designConfirmed": True, "planConfirmedAt": 1},
        "counterfactualDesign": {"schemaVersion": 2, "groups": groups, "controlledVariables": "流量和模型保持一致", "unit": "独立仿真种子", "scope": "全程全局", "repeatCount": 3, "executionSettings": deepcopy(settings)},
        "counterfactualSettings": {"repeatCount": 3}, "executionSettings": settings,
        "variableDefinitions": {"factors": [definition(symbol) for symbol in ("T_start", "Compliance", "Delay")], "responses": [dict(definition("risk_time"), id="risk_exposure")]},
        "artificialSociety": {"schemaVersion": 1, "baseModel": "memorial-post-striping", "overrides": {"agents": "", "environment": "", "rules": ""}}}
    return dataset, plan


class UnifiedDesignTests(unittest.TestCase):
    def test_one_group_is_enough_without_three_design_confirmations(self):
        _, plan = fixture()
        plan["counterfactualDesign"]["groups"] = plan["counterfactualDesign"]["groups"][:1]
        validate_review_intake(plan)
        self.assertNotIn("experimentSchemeDesign", plan)
        for field in ("baselineConfirmed", "goalConfirmed", "societyConfirmed", "designConfirmed"):
            invalid = deepcopy(plan)
            invalid["reviewIntake"][field] = False
            with self.assertRaises(ApiError):
                validate_review_intake(invalid)

    def test_invalid_references_controls_and_endpoints_are_rejected(self):
        _, plan = fixture()
        for mutate in (
            lambda p: p["counterfactualDesign"]["groups"][0]["execution"].update(referenceDesignId="CF-02"),
            lambda p: p["counterfactualDesign"]["groups"][1]["execution"].update(compliance=.1, startSeconds=14400),
            lambda p: p["counterfactualDesign"]["groups"][0].update(factorSymbols=["T_start"]),
            lambda p: p["counterfactualDesign"]["groups"][0].update(primaryMetricId="unknown"),
            lambda p: p["counterfactualDesign"].update(groups=None),
            lambda p: p["counterfactualDesign"]["groups"][0].update(method=[]),
            lambda p: p["counterfactualDesign"]["groups"][0].update(scheme_type="observation"),
        ):
            invalid = deepcopy(plan); mutate(invalid)
            with self.subTest(mutate=mutate), self.assertRaises(ApiError):
                validate_review_intake(invalid)

    def test_saved_snapshot_reads_back_and_freezes_the_unified_execution(self):
        dataset, plan = fixture()
        stored = {}
        def save(user, workspace, draft_id, data):
            stored.update(id="p1", **deepcopy(data)); return deepcopy(stored)
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        app.repo = SimpleNamespace(get=lambda kind, *args: deepcopy(dataset if kind == "datasets" else stored),
            save_review_plan=save, event=lambda *args, **kwargs: None,
            create=lambda kind, user, workspace, data: dict(id="exp-1", **deepcopy(data)))
        receipt = app.create_review_plan("u", "w", {"name": plan["label"], "conversationId": "c1", "datasetId": dataset["id"], "datasetVersion": dataset["version"],
            "plan": plan, "calibration": {"score": None, "threshold": 80, "confirmed": True}, "calibrationTargets": {}})
        request = execution_template(dataset, receipt["plan"]); request["reviewPlanId"] = "p1"
        task = app.create_experiment("u", "w", request)
        self.assertEqual(4, len(task["groups"]))
        self.assertTrue(all(group["experimentType"] == "counterfactual" for group in task["groups"]))
        self.assertEqual(plan["counterfactualDesign"], task["reviewSnapshot"]["counterfactualDesign"])
        altered = deepcopy(request); altered["groups"][0]["interventions"][0]["atSeconds"] += 1
        with self.assertRaises(ApiError):
            app.create_experiment("u", "w", altered)

    def test_custom_society_changes_are_saved_but_cannot_be_ignored_by_execution(self):
        dataset, plan = fixture()
        plan["artificialSociety"]["overrides"]["rules"] = "改变拥挤规则"
        validate_review_intake(plan)
        with self.assertRaisesRegex(ApiError, "模型改动"):
            execution_template(dataset, plan)

    def test_analysis_reuses_eligible_groups_across_the_three_sections(self):
        dataset, plan = fixture()
        task = execution_template(dataset, plan); task["reviewSnapshot"] = plan
        runs = [{"id": f"{id_}-{seed}", "planId": id_, "seed": seed, "status": "completed", "metrics": {metric: 1 for metric in task["metricIds"]}}
                for id_ in ["F-00", *[group["id"] for group in task["groups"]]] for seed in range(3)]
        result = summarize(task, runs)
        ids = lambda phase: {row["groupId"] for row in result[phase]["experiments"]}
        self.assertEqual({"CF-01", "CF-02", "CF-03", "CF-04"}, ids("observation"))
        self.assertEqual({"CF-02", "CF-03", "CF-04"}, ids("intervention"))
        self.assertEqual({"CF-04"}, ids("mechanism"))
        self.assertEqual("insufficient_data", result["causalGraph"]["status"])
        self.assertTrue(all(effect["pairCount"] == 3 for effect in result["comparison"]["effects"]))

    def test_unified_agent_accepts_pool_advice_and_rejects_type_partitions(self):
        _, plan = fixture()
        context = {"designSchemaVersion": 2, "executionCapabilities": True, "counterfactualDesign": plan["counterfactualDesign"], "variableDefinitions": plan["variableDefinitions"]}
        reply = {"assistant_message": "请核对统一实验池", "groups": plan["counterfactualDesign"]["groups"], "repeat_count": 3}
        validate_reply("design", reply, context)
        with self.assertRaises(ValueError):
            validate_reply("design", {"assistant_message": "三类设计", "experiments": []}, context)
        context["executionCapabilities"] = False
        with self.assertRaises(ValueError):
            validate_reply("design", reply, context)

    def test_actual_agent_and_skills_load_the_v2_reply_contract(self):
        _, plan = fixture()
        seen = []
        def complete(messages, model, budget):
            seen.append(messages[0]["content"])
            return {"assistant_message": "请核对统一实验池", "groups": plan["counterfactualDesign"]["groups"]}
        result = respond({"task": "design", "messages": [{"role": "user", "content": "请核对变量、假设和实验组名称"}],
            "context": {"designSchemaVersion": 2, "goalConfirmed": True, "baselineConfirmed": True, "executionCapabilities": True,
                "counterfactualDesign": plan["counterfactualDesign"], "variableDefinitions": plan["variableDefinitions"]}}, complete=complete)
        self.assertEqual("PostAgent", result["agent"])
        self.assertIn(UNIFIED_DESIGN_CONTRACT, seen[0])
        self.assertFalse(result["toolsExecuted"])
        self.assertEqual(4, len(result["reply"]["groups"]))

    def test_incremental_group_addition_preserves_the_rest_of_the_pool(self):
        _, plan = fixture()
        context = {"designSchemaVersion": 2, "executionCapabilities": True, "counterfactualDesign": deepcopy(plan["counterfactualDesign"]), "variableDefinitions": plan["variableDefinitions"]}
        new_group = {**deepcopy(context["counterfactualDesign"]["groups"][0]), "id": "CF-05", "name": "新增验证组"}
        before = deepcopy(context)
        reply = validate_reply("design", {"assistant_message": "新增验证组", "action": "update_design", "group_updates": [new_group]}, context)
        self.assertEqual("update_design", reply["action"])
        self.assertEqual(before, context)
        validate_reply("design", {"assistant_message": "仅更新时点", "group_updates": [{"id": "CF-02", "execution": {"startSeconds": 13500}}]}, context)
        self.assertEqual(before, context)
        with self.assertRaises(ValueError):
            validate_reply("design", {"assistant_message": "改变参照组导致消融不再单因素", "group_updates": [{"id": "CF-01", "execution": {"startSeconds": 13500}}]}, context)
        with self.assertRaises(ValueError):
            validate_reply("design", {"assistant_message": "已新增", "action": "update_design"}, context)
        with self.assertRaises(ValueError):
            validate_reply("design", {"assistant_message": "新增", "group_updates": [new_group, new_group]}, context)
