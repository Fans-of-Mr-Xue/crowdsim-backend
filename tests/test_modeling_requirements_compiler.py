from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.artificial_society_repository import ArtificialSocietyRepository
from crowdsim.modeling_algorithm.compiler import (
    ModelingRequirementsError,
    build_artificial_society,
    default_modeling_requirements,
    environment_candidates,
    executable_catalog,
    validate_modeling_requirements,
    compile_artificial_society,
    rule_recommendations,
    strong_rule_catalogue,
)
from crowdsim.modeling_algorithm.rule_model.catalogue import (
    STRONG_RULES,
    default_rule_methods,
    implemented_rule_method,
    resolve_rule_method,
)
from crowdsim.modeling_algorithm.individual_model.pipeline import RolePipelineConfig

import importlib.util


def requirement_payload(location_id="memorial-tower"):
    preset = json.loads((Path(__file__).resolve().parents[1] / "config/during_presets/memorial_tower_v1.json").read_text())
    spatial = deepcopy(preset["requirement"]["spatial_scope"])
    spatial["location_resolution"] = {
        "provider": "amap", "confirmed": True,
        "confirmed_at": "2026-10-15T00:00:00Z", "queried_at": "2026-10-15T00:00:00Z",
        "original_query": spatial["name"], "name": spatial["name"], "address": spatial["address"],
        "poi_id": "test-poi", "crs": "EPSG:4326", "center": list(spatial["center"]),
        "provider_crs": "GCJ-02", "provider_center": [121.4911, 31.2422], "adcode": "310101",
    }
    spatial["location_id"] = location_id
    return {
        "schema_version": 2,
        "project": {"title": "建模编译测试", "description": "验证建模需求由后端编译"},
        "research": {"problem_type": 1},
        "spatial_scope": spatial,
        "population": {"total": 10, "initial_distribution": "uniform", "distributions": {
            "crowd_role": [{"code": "commuter", "label": "通勤者", "percent": 100}],
            "age_band": [{"code": "age_18_34", "label": "18-34", "percent": 100}],
            "origin": [{"id": "local", "label": "本地居民", "percent": 100}],
            "gender": [{"code": "female", "label": "女性", "percent": 100}],
        }},
        "scenario": {"event_category": {"id": "culture", "label": "文化活动"}, "additional_events": [], "hotspot_start_seconds": 10},
        "observation": {"metric_ids": ["global-density"], "local_partition_mode": "uniform", "selection_schema_version": 2},
        "experiment": {"duration_seconds": 60, "repetitions": 1, "baseline_control": "none"},
    }


def configured_modeling(requirement):
    modeling = default_modeling_requirements(requirement)
    modeling["environment"]["candidates"] = environment_candidates(requirement)
    modeling["environment"]["selected_environment_model_id"] = "env-memorial-tower-v1"
    agent = modeling["individual"]["agent_types"][0]
    agent["capabilities"] = {"emotion": True, "game": True, "learning": False}
    # Emotion method lives in emotionState; architecture carries no emotion/desire module.
    agent["emotionState"] = {"method": "PAD 连续情感模型", "participates_in_operation": False}
    agent["architecture"] = [
        {"id": "perception", "method": "运行时局部观察"},
        {"id": "memory", "method": "运行时有限状态"},
        {"id": "goal", "method": "运行时目标与路线"},
        {"id": "game", "method": "有限理性博弈"},
        {"id": "decision", "method": "LLM-driven"},
        {"id": "action", "method": "SUMO striping"},
    ]
    modeling["rules"]["accepted_recommended_rule_ids"] = list(modeling["rules"]["recommended_rule_ids"])
    modeling["rules"]["rejected_recommended_rule_ids"] = []
    return confirm_modeling(modeling)


def confirm_modeling(modeling):
    for section in ("individual", "environment", "rules"):
        content = deepcopy(modeling[section])
        if section == "environment":
            content.pop("candidates", None)
        modeling["decisions"][section] = {"status": "accepted", "source": "test-user", "fingerprint": json.dumps(content, ensure_ascii=False)}
    return modeling


class ModelingRequirementsCompilerTests(unittest.TestCase):
    def test_full_agent_architecture_requires_desire_before_goal(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        agent = modeling["individual"]["agent_types"][0]
        agent["capabilities"] = {"emotion": True, "game": True, "learning": True}
        agent["emotionState"] = {"method": "PAD 连续情感模型", "participates_in_operation": True}
        agent["architecture"] = [
            {"id": "perception", "method": "运行时局部观察"},
            {"id": "memory", "method": "运行时有限状态"},
            {"id": "desire", "method": "情感驱动欲望"},
            {"id": "goal", "method": "运行时目标与路线"},
            {"id": "game", "method": "有限理性博弈"},
            {"id": "decision", "method": "LLM-driven"},
            {"id": "action", "method": "SUMO striping"},
            {"id": "learning", "method": "经验回放"},
        ]
        confirm_modeling(modeling)
        validated = validate_modeling_requirements(requirement, modeling)
        self.assertEqual(
            ["perception", "memory", "desire", "goal", "game", "decision", "action", "learning"],
            [item["id"] for item in validated["individual"]["agent_types"][0]["architecture"]],
        )
        compiled = compile_artificial_society(
            {"requirement": requirement, "requirement_id": "req-test", "fingerprint": "sha256:test"},
            modeling,
        )
        self.assertEqual(
            ["perception", "memory", "desire", "goal", "game", "decision", "action", "learning"],
            [item["id"] for item in compiled["individual_model"]["agent_types"][0]["architecture"]],
        )

        wrong = deepcopy(modeling)
        architecture = wrong["individual"]["agent_types"][0]["architecture"]
        architecture[2], architecture[3] = architecture[3], architecture[2]
        confirm_modeling(wrong)
        with self.assertRaisesRegex(ModelingRequirementsError, "perception-memory-desire-goal"):
            validate_modeling_requirements(requirement, wrong)

    def test_optional_modules_must_match_confirmed_capabilities(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        agent = modeling["individual"]["agent_types"][0]
        agent["capabilities"]["game"] = False
        confirm_modeling(modeling)
        with self.assertRaisesRegex(ModelingRequirementsError, "game module must match"):
            validate_modeling_requirements(requirement, modeling)

    def test_catalog_exposes_only_real_executable_methods_for_selection(self):
        catalog = executable_catalog()
        self.assertTrue(all(item["implemented"] for item in catalog))
        self.assertTrue(any(item["method"] == "PAD 连续情感模型" for item in catalog))
        self.assertFalse(any(item["method"] == "OCC 事件评价模型" for item in catalog))

    def test_environment_candidates_require_registered_runtime_asset(self):
        candidates = environment_candidates(requirement_payload())
        self.assertTrue(candidates)
        self.assertGreaterEqual(candidates[0]["match_score"], 60)
        self.assertIn("研究地点", candidates[0]["match_reason"])
        self.assertTrue(candidates[0]["match_dimensions"]["event_category"])
        self.assertEqual([], environment_candidates(requirement_payload("east-nanjing-road")))
        self.assertEqual([], environment_candidates(requirement_payload("unregistered-place")))

    def test_rule_recommendations_come_from_executable_requirement_features(self):
        recommendations = rule_recommendations(requirement_payload())
        self.assertEqual(["hotspot-activity-schedule", "congestion-route-choice"], [item["id"] for item in recommendations])
        self.assertTrue(all(item["implemented"] for item in recommendations))
        self.assertTrue(all(item["runtime_method"] for item in recommendations))

    def test_strong_rule_catalogue_lists_six_described_executable_methods(self):
        catalogue = strong_rule_catalogue()
        self.assertEqual([item["id"] for item in STRONG_RULES],
                         [item["id"] for item in catalogue])
        self.assertEqual(6, len(catalogue))
        for entry in catalogue:
            self.assertTrue(entry["summary"].strip())
            self.assertTrue(entry["default_method"].strip())
            self.assertTrue(entry["runtime_consumed"])
            labels = {method["label"]: method for method in entry["methods"]}
            self.assertIn(entry["default_method"], labels)
            self.assertTrue(entry["methods"])
            for method in entry["methods"]:
                self.assertTrue(method["implemented"])
                self.assertTrue(method["description"].strip())
                self.assertTrue(method["runtime_component"].strip())
                self.assertTrue(method["module_path"].strip())
                # The advertised backing module must really exist (no fake option).
                self.assertIsNotNone(importlib.util.find_spec(method["module_path"]),
                                     method["module_path"])

    def test_default_rule_methods_match_catalogue_executable_methods(self):
        defaults = default_rule_methods()
        self.assertEqual(set(defaults), {item["id"] for item in STRONG_RULES})
        for rule_id, method in defaults.items():
            self.assertTrue(implemented_rule_method(rule_id, method))
            resolved = resolve_rule_method(rule_id, method)
            self.assertEqual(method, resolved["label"])
            self.assertTrue(resolved["implemented"])

    def test_executable_catalog_exposes_every_strong_rule_method(self):
        by_id = {item["rule_id"]: item for item in executable_catalog() if item.get("model") == "rule"}
        self.assertEqual({item["id"] for item in STRONG_RULES}, set(by_id))
        for entry in STRONG_RULES:
            exposed = by_id[entry["id"]]
            self.assertTrue(exposed["implemented"])
            self.assertEqual(entry["default_method"], exposed["method"])
            self.assertTrue(exposed["summary"])
            self.assertTrue(exposed["runtime_component"])
            self.assertTrue(exposed["module_path"])

    def test_compiled_strong_rules_carry_method_summary_and_runtime_component(self):
        requirement = requirement_payload()
        payload = compile_artificial_society(
            {"requirement": requirement, "requirement_id": "req-test", "fingerprint": "sha256:test"},
            configured_modeling(requirement),
        )
        rules = {rule["id"]: rule for rule in payload["rule_model"]["rules"]}
        for entry in STRONG_RULES:
            rule = rules[entry["id"]]
            self.assertEqual(entry["default_method"], rule["method"])
            self.assertEqual(entry["summary"], rule["summary"])
            self.assertTrue(rule["runtime_component"])
            self.assertTrue(rule["module_path"])
            self.assertEqual("runtime_consumed", rule["status"])
            self.assertTrue(rule["selected"])

    def test_valid_rule_method_override_compiles_through(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        # Re-select the catalogue default explicitly; the compiler must accept it.
        for rule_id, method in default_rule_methods().items():
            modeling["rules"]["rule_methods"][rule_id] = method
        confirm_modeling(modeling)
        payload = compile_artificial_society(
            {"requirement": requirement, "requirement_id": "req-test", "fingerprint": "sha256:test"},
            modeling,
        )
        rules = {rule["id"]: rule for rule in payload["rule_model"]["rules"]}
        for rule_id, method in default_rule_methods().items():
            self.assertEqual(method, rules[rule_id]["method"])

    def test_fake_rule_method_is_rejected_by_compiler(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        modeling["rules"]["rule_methods"]["visual-state"] = "不存在的状态算法"
        confirm_modeling(modeling)
        with self.assertRaisesRegex(ModelingRequirementsError, "not executable"):
            validate_modeling_requirements(requirement, modeling)

    def test_unknown_rule_id_in_rule_methods_is_rejected(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        modeling["rules"]["rule_methods"]["made-up-rule"] = "任意方法"
        confirm_modeling(modeling)
        with self.assertRaisesRegex(ModelingRequirementsError, "unknown strong rules"):
            validate_modeling_requirements(requirement, modeling)

    def test_placeholder_rule_module_is_never_advertised(self):
        # The reserved placeholder files stay unimplemented and must not appear
        # as selectable catalogue methods.
        advertised = {item["label"] for entry in STRONG_RULES for item in entry["methods"]}
        self.assertNotIn("可视状态", advertised)
        self.assertFalse(implemented_rule_method("visual-state", "可视状态"))

    def test_default_and_stale_confirmation_are_rejected(self):
        requirement = requirement_payload()
        with self.assertRaisesRegex(ModelingRequirementsError, "explicit user confirmation"):
            validate_modeling_requirements(requirement, default_modeling_requirements(requirement))
        modeling = configured_modeling(requirement)
        modeling["rules"]["decision_period_seconds"] = 3
        with self.assertRaisesRegex(ModelingRequirementsError, "changed after confirmation"):
            validate_modeling_requirements(requirement, modeling)

    def test_rejected_optional_rule_compiles_to_runtime_switch(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        modeling["rules"]["accepted_recommended_rule_ids"].remove("congestion-route-choice")
        modeling["rules"]["rejected_recommended_rule_ids"] = ["congestion-route-choice"]
        confirm_modeling(modeling)
        payload = compile_artificial_society({"requirement": requirement, "requirement_id": "req-test", "fingerprint": "sha256:test"}, modeling)
        self.assertFalse(payload["rule_model"]["runtime_parameters"]["congestion_route_choice"])
        self.assertNotIn("congestion-route-choice", [item["id"] for item in payload["rule_model"]["rules"]])

    def test_compiled_model_uses_autonomous_people_and_runtime_services(self):
        requirement = requirement_payload()
        payload = compile_artificial_society({"requirement": requirement, "requirement_id": "req-test", "fingerprint": "sha256:test"}, configured_modeling(requirement))
        self.assertEqual(["pedestrian"], [role["id"] for role in payload["individual_model"]["roles"]])
        self.assertEqual(["information-delivery", "intervention-executor"], payload["individual_model"]["runtime_components"])

    def test_basic_diagram_is_compiled_only_with_valid_parameters(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        modeling["environment"]["density_response"] = {"method": "基本图模型", "critical_density": 4.0, "minimum_speed_ratio": .1}
        confirm_modeling(modeling)
        payload = compile_artificial_society({"requirement": requirement, "requirement_id": "req-test", "fingerprint": "sha256:test"}, modeling)
        self.assertEqual("基本图模型", payload["environment_model"]["spatial_structure"]["density_response"]["method"])
        self.assertEqual("基本图模型", payload["environment_model"]["spatial_structure"]["responses"][0]["method"])
        modeling["environment"]["density_response"]["critical_density"] = 0
        confirm_modeling(modeling)
        with self.assertRaisesRegex(ModelingRequirementsError, "parameters are invalid"):
            compile_artificial_society({"requirement": requirement, "requirement_id": "req-test", "fingerprint": "sha256:test"}, modeling)

    def test_build_compiles_model_and_returns_ready_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = RequirementRepository(f"{directory}/requirements")
            record = requirements.create(RequirementSpec.parse(requirement_payload()))
            models = ArtificialSocietyRepository(f"{directory}/models", requirements=requirements)
            result = build_artificial_society({
                "conversationId": "check", "snapshotId": "snapshot-1",
                "requirementId": record["requirement_id"], "requirementFingerprint": record["fingerprint"],
                "modelingRequirements": configured_modeling(record["requirement"]),
            }, "during-check", requirements=requirements, models=models)
            self.assertEqual("READY", result["execution_manifest"]["status"])
            self.assertEqual(result["model_id"], result["execution_manifest"]["modelId"])
            self.assertRegex(result["execution_manifest"]["experimentConfigId"], r"^exp-config-[0-9a-f]{16}$")
            self.assertEqual(["validate", "individual", "environment", "rules", "manifest"], [step["id"] for step in result["build_steps"]])
            self.assertTrue(all(step["status"] == "completed" for step in result["build_steps"]))
            agent0 = result["artificial_society"]["individual_model"]["agent_types"][0]
            self.assertEqual("PAD 连续情感模型", agent0["emotionState"]["method"])
            self.assertNotIn("emotion", {item["id"] for item in agent0["architecture"]})

    def test_emotion_alone_compiles_without_desire_module(self):
        requirement = requirement_payload()
        payload = compile_artificial_society(
            {"requirement": requirement, "requirement_id": "req-test", "fingerprint": "sha256:test"},
            configured_modeling(requirement),
        )
        agent0 = payload["individual_model"]["agent_types"][0]
        self.assertEqual("PAD 连续情感模型", agent0["emotionState"]["method"])
        self.assertFalse(agent0["emotionState"]["participates_in_operation"])
        arch_ids = {item["id"] for item in agent0["architecture"]}
        self.assertNotIn("emotion", arch_ids)
        self.assertNotIn("desire", arch_ids)

    def test_emotion_participates_accepts_desire_module(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        agent = modeling["individual"]["agent_types"][0]
        agent["emotionState"] = {"method": "PAD 连续情感模型", "participates_in_operation": True}
        goal_index = next(i for i, item in enumerate(agent["architecture"]) if item["id"] == "goal")
        agent["architecture"].insert(goal_index, {"id": "desire", "method": "情感驱动欲望"})
        confirm_modeling(modeling)
        payload = compile_artificial_society(
            {"requirement": requirement, "requirement_id": "req-test", "fingerprint": "sha256:test"},
            modeling,
        )
        arch_ids = {item["id"] for item in payload["individual_model"]["agent_types"][0]["architecture"]}
        self.assertIn("desire", arch_ids)
        self.assertNotIn("emotion", arch_ids)

    def test_desire_module_without_participation_is_rejected(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        modeling["individual"]["agent_types"][0]["architecture"].append(
            {"id": "desire", "method": "情感驱动欲望"})
        confirm_modeling(modeling)
        with self.assertRaisesRegex(ModelingRequirementsError, "participates_in_operation"):
            validate_modeling_requirements(requirement, modeling)

    def test_game_and_learning_remain_architecture_modules(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        agent = modeling["individual"]["agent_types"][0]
        agent["capabilities"] = {"emotion": True, "game": True, "learning": True}
        agent["architecture"] = [
            {"id": "perception", "method": "运行时局部观察"},
            {"id": "memory", "method": "运行时有限状态"},
            {"id": "goal", "method": "运行时目标与路线"},
            {"id": "game", "method": "有限理性博弈"},
            {"id": "decision", "method": "LLM-driven"},
            {"id": "action", "method": "SUMO striping"},
            {"id": "learning", "method": "经验回放"},
        ]
        confirm_modeling(modeling)
        payload = compile_artificial_society(
            {"requirement": requirement, "requirement_id": "req-test", "fingerprint": "sha256:test"},
            modeling,
        )
        arch_ids = {item["id"] for item in payload["individual_model"]["agent_types"][0]["architecture"]}
        self.assertIn("game", arch_ids)
        self.assertIn("learning", arch_ids)

    def test_build_rejects_placeholder_algorithm(self):
        requirement = requirement_payload()
        modeling = configured_modeling(requirement)
        modeling["individual"]["agent_types"][0]["architecture"][2]["method"] = "OCC 事件评价模型"
        confirm_modeling(modeling)
        with tempfile.TemporaryDirectory() as directory:
            requirements = RequirementRepository(f"{directory}/requirements")
            record = requirements.create(RequirementSpec.parse(requirement))
            models = ArtificialSocietyRepository(f"{directory}/models", requirements=requirements)
            with self.assertRaisesRegex(ModelingRequirementsError, "not executable"):
                build_artificial_society({
                    "conversationId": "check", "snapshotId": "snapshot-1",
                    "requirementId": record["requirement_id"], "requirementFingerprint": record["fingerprint"],
                    "modelingRequirements": modeling,
                }, "during-check", requirements=requirements, models=models)

    def test_enhanced_chain_runs_without_replay_when_learning_disabled(self):
        methods = {"emotion": "PAD 连续情感模型", "desire": "情感驱动欲望", "game": "有限理性博弈", "decision": "LLM-driven"}
        config = RolePipelineConfig("commuter", methods, {"emotion": True, "game": True, "learning": False})
        self.assertTrue(config.enabled)
        self.assertFalse(config.replay_enabled)
        learned = deepcopy(methods); learned["learning"] = "经验回放"
        config = RolePipelineConfig("commuter", learned, {"emotion": True, "game": True, "learning": True})
        self.assertTrue(config.enabled)
        self.assertTrue(config.replay_enabled)


if __name__ == "__main__":
    unittest.main()
