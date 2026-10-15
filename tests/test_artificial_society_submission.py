import tempfile
import unittest

from crowdsim.domain.artificial_society_spec import (
    ArtificialSocietySpec,
    ArtificialSocietyValidationError,
)
from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.infrastructure.artificial_society_repository import ArtificialSocietyRepository
from crowdsim.infrastructure.requirement_repository import RequirementRepository


def requirement_payload():
    return {
        "schema_version": 1,
        "project": {"title": "外滩人群推演", "description": "验证人工社会版本链"},
        "spatial_scope": {
            "source": "preset", "location_id": "memorial-tower", "name": "人民英雄纪念塔",
            "crs": "EPSG:4326", "center": [121.4869, 31.2442],
            "boundary": {"type": "Polygon", "coordinates": [[
                [121.4860, 31.2430], [121.4870, 31.2430],
                [121.4870, 31.2440], [121.4860, 31.2430],
            ]]},
        },
        "population": {"total": 10, "distributions": {
            "crowd_role": [{"code": "commuter", "label": "通勤者", "percent": 100}],
            "age_band": [{"code": "age_18_34", "label": "18-34", "percent": 100}],
            "origin": [{"id": "origin-1", "label": "本地居民", "percent": 100}],
            "gender": [{"code": "female", "label": "女性", "percent": 100}],
        }},
        "scenario": {"event_category": {"id": "culture", "code": "E1", "label": "文化娱乐活动"}},
        "observation": {"metric_ids": ["global-speed"], "local_partition_mode": "uniform"},
    }


def artificial_society_payload(requirement_record):
    requirement = requirement_record["requirement"]
    roles = [{"id": value, "selected": True} for value in ("pedestrian", "signal", "policy")]
    layers = [{"id": value, "selected": True} for value in ("topology", "dynamic", "information", "intervention")]
    rules = [{"id": value, "selected": True} for value in (
        "population-allocation", "sumo-motion", "local-observation",
        "decision-scheduling", "visual-state", "message-control",
    )]
    return {
        "schema_version": 2,
        "source": "manual",
        "requirement_id": requirement_record["requirement_id"],
        "requirement_fingerprint": requirement_record["fingerprint"],
        "overview": {"title": "手动人工社会"},
        "individual_model": {
            "population_total": requirement["population"]["total"],
            "distributions": requirement["population"]["distributions"],
            "profile_parameters": {
                "free_walking_speed": {"min": 1.0, "max": 1.6, "floor": 0.5, "ceiling": 2.5},
                "perception_radius": {"min": 6.0, "max": 18.0, "floor": 2.0, "ceiling": 30.0},
                "crowding_tolerance": {"min": 0.0, "max": 1.0, "floor": 0.0, "ceiling": 1.0},
                "following_tendency": {"min": 0.0, "max": 1.0, "floor": 0.0, "ceiling": 1.0},
                "authority_compliance": {"min": 0.0, "max": 1.0, "floor": 0.0, "ceiling": 1.0},
            },
            "roles": roles,
            "agent_types": [{
                "id": "commuter", "name": "通勤者", "proportion": 100,
                "attributes": {"speed": {"min": 1.0, "max": 1.6}},
                "capabilities": {"emotion": True, "game": True, "learning": False},
                "architecture": [
                    {"id": "perception", "selected": True, "method": "局部邻域观测"},
                    {"id": "emotion", "selected": True, "method": "PAD 连续情感模型"},
                    {"id": "desire", "selected": True, "method": "情感驱动欲望"},
                    {"id": "goal", "selected": True, "method": "BDI 目标栈"},
                    {"id": "game", "selected": True, "method": "有限理性博弈"},
                    {"id": "decision", "selected": True, "method": "效用最大化"},
                    {"id": "action", "selected": True, "method": "SUMO striping"},
                ],
            }],
        },
        "environment_model": {
            "scenario_binding": {"location_id": requirement["spatial_scope"]["location_id"]},
            "layers": layers,
            "spatial_structure": {
                "network_name": "纪念塔路网", "network_method": "SUMO 路网拓扑",
                "entrances": [{"id": "in-1", "name": "入口 1", "capacity_per_minute": 120, "state": "开放"}],
                "exits": [{"id": "out-1", "name": "出口 1", "capacity_per_minute": 150, "state": "开放"}],
                "factors": [{"id": "density", "name": "局部人群密度", "selected": True, "initial": 0}],
                "responses": [{"id": "density-feedback", "name": "密度—速度反馈", "selected": True, "theory": "基本图模型", "method": "分段函数"}],
            },
        },
        "rule_model": {
            "rules": rules,
            "runtime_parameters": {
                "decision_period_seconds": 3.0,
                "visual_state": {
                    "busy_density_person_per_m2": 0.6,
                    "crowded_density_person_per_m2": 1.6,
                    "stalled_high_risk_density_person_per_m2": 3.6,
                    "critical_density_person_per_m2": 4.2,
                    "low_speed_threshold_mps": 0.25,
                    "confirmation_duration_seconds": 6.0,
                },
            },
        },
    }


class ArtificialSocietySubmissionTests(unittest.TestCase):
    def test_version_round_trip_and_requirement_consistency(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = RequirementRepository(f"{directory}/requirements")
            requirement = requirements.create(RequirementSpec.parse(requirement_payload()))
            models = ArtificialSocietyRepository(f"{directory}/models", requirements=requirements)
            spec = ArtificialSocietySpec.parse(artificial_society_payload(requirement))
            record = models.create(spec)
            self.assertEqual(record, models.load(record["model_id"]))
            self.assertEqual(requirement["requirement_id"], record["requirement_id"])
            self.assertEqual("runtime_consumed", record["capabilities"]["profile_parameters"])
            self.assertEqual("runtime_consumed", record["capabilities"]["agent_types"])
            self.assertEqual("runtime_consumed_for_implemented_chain", record["capabilities"]["agent_module_methods"])
            self.assertEqual("published_environment_contract", record["capabilities"]["environment_responses"])
            self.assertEqual("PAD 连续情感模型", record["artificial_society"]["individual_model"]["agent_types"][0]["architecture"][1]["method"])

    def test_emotion_capability_requires_emotion_and_desire_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = RequirementRepository(f"{directory}/requirements")
            requirement = requirements.create(RequirementSpec.parse(requirement_payload()))
            payload = artificial_society_payload(requirement)
            payload["individual_model"]["agent_types"][0]["architecture"] = [
                item for item in payload["individual_model"]["agent_types"][0]["architecture"]
                if item["id"] != "desire"
            ]
            with self.assertRaisesRegex(ArtificialSocietyValidationError, "emotion capability"):
                ArtificialSocietySpec.parse(payload)

    def test_unknown_agent_method_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = RequirementRepository(f"{directory}/requirements")
            requirement = requirements.create(RequirementSpec.parse(requirement_payload()))
            payload = artificial_society_payload(requirement)
            payload["individual_model"]["agent_types"][0]["architecture"][1]["method"] = "不存在的情感算法"
            with self.assertRaisesRegex(ArtificialSocietyValidationError, "unsupported agent method"):
                ArtificialSocietySpec.parse(payload)

    def test_environment_response_requires_theory_and_method(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = RequirementRepository(f"{directory}/requirements")
            requirement = requirements.create(RequirementSpec.parse(requirement_payload()))
            payload = artificial_society_payload(requirement)
            payload["environment_model"]["spatial_structure"]["responses"][0]["theory"] = ""
            with self.assertRaisesRegex(ArtificialSocietyValidationError, "theory and method"):
                ArtificialSocietySpec.parse(payload)

    def test_reversed_profile_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = RequirementRepository(f"{directory}/requirements")
            requirement = requirements.create(RequirementSpec.parse(requirement_payload()))
            payload = artificial_society_payload(requirement)
            payload["individual_model"]["profile_parameters"]["perception_radius"].update(min=20.0, max=10.0)
            with self.assertRaisesRegex(ArtificialSocietyValidationError, "outside allowed bounds"):
                ArtificialSocietySpec.parse(payload)

    def test_visual_threshold_order_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = RequirementRepository(f"{directory}/requirements")
            requirement = requirements.create(RequirementSpec.parse(requirement_payload()))
            payload = artificial_society_payload(requirement)
            payload["rule_model"]["runtime_parameters"]["visual_state"]["crowded_density_person_per_m2"] = 0.2
            with self.assertRaisesRegex(ArtificialSocietyValidationError, "strictly increasing"):
                ArtificialSocietySpec.parse(payload)


if __name__ == "__main__":
    unittest.main()
