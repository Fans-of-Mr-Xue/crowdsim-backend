import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from crowdsim.domain.population_profiles import PopulationProfileSampler
from crowdsim.domain.requirement_spec import (
    RequirementSpec,
    RequirementValidationError,
    requirement_runtime_summary,
)
from crowdsim.infrastructure.requirement_repository import RequirementNotFoundError, RequirementRepository

try:
    from crowdsim.infrastructure.websocket_server import OverlayServer
except ModuleNotFoundError:
    OverlayServer = None


def requirement_payload(location_id="memorial-tower"):
    return {
        "schema_version": 1,
        "project": {"title": "外滩人群推演", "description": "验证需求提交链路"},
        "spatial_scope": {
            "source": "preset",
            "location_id": location_id,
            "location_code": "BUND-01",
            "name": "人民英雄纪念塔",
            "crs": "EPSG:4326",
            "center": [121.4869, 31.2442],
            "boundary": {
                "type": "Polygon",
                "coordinates": [[
                    [121.4860, 31.2430],
                    [121.4870, 31.2430],
                    [121.4870, 31.2440],
                    [121.4860, 31.2430],
                ]],
            },
        },
        "population": {
            "total": 10,
            "distributions": {
                "crowd_role": [
                    {"code": "commuter", "label": "通勤者", "percent": 30},
                    {"code": "leisure_visitor", "label": "休闲游客", "percent": 70},
                ],
                "age_band": [
                    {"code": "age_18_34", "label": "18-34", "percent": 50},
                    {"code": "age_35_54", "label": "35-54", "percent": 50},
                ],
                "origin": [
                    {"id": "origin-1", "label": "本地居民", "percent": 60},
                    {"id": "origin-2", "label": "外地游客", "percent": 40},
                ],
                "gender": [
                    {"code": "female", "label": "女性", "percent": 50},
                    {"code": "male", "label": "男性", "percent": 50},
                ],
            },
        },
        "scenario": {
            "event_category": {"id": "culture", "code": "E1", "label": "文化娱乐活动"},
        },
        "observation": {
            "metric_ids": ["global-speed", "local-density"],
            "local_partition_mode": "uniform",
        },
    }


class CapturingClient:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class RequirementSubmissionTests(unittest.IsolatedAsyncioTestCase):
    def test_reserved_default_id_loads_a_copy_and_keeps_normal_ids_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = RequirementRepository(directory)
            original = repository.create(RequirementSpec.parse(requirement_payload()))
            copied = {**original, "requirement_id": "req-default"}
            destination = Path(directory) / "req-default.json"
            destination.write_text(json.dumps(copied, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(copied, repository.load("req-default"))
            self.assertEqual(original, repository.load(original["requirement_id"]))
            for invalid in ["req-other", "req-default-extra", "../req-default", "req-../default", "REQ-default", None]:
                with self.subTest(invalid=invalid), self.assertRaises(RequirementNotFoundError):
                    repository.load(invalid)

    def test_default_copy_still_requires_matching_id_and_valid_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = RequirementRepository(directory)
            original = repository.create(RequirementSpec.parse(requirement_payload()))
            destination = Path(directory) / "req-default.json"
            destination.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "id mismatch"):
                repository.load("req-default")
            copied = {**original, "requirement_id": "req-default", "fingerprint": "sha256:invalid"}
            destination.write_text(json.dumps(copied), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                repository.load("req-default")

    def test_validation_and_immutable_file_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = RequirementRepository(directory)
            record = repository.create(RequirementSpec.parse(requirement_payload()))
            loaded = repository.load(record["requirement_id"])
            self.assertEqual(record, loaded)
            self.assertTrue((Path(directory) / f'{record["requirement_id"]}.json').is_file())
            self.assertEqual("supported", record["capabilities"]["location"])

    def test_invalid_distribution_is_rejected(self):
        payload = requirement_payload()
        payload["population"]["distributions"]["gender"][0]["percent"] = 40
        with self.assertRaisesRegex(RequirementValidationError, "must total 100"):
            RequirementSpec.parse(payload)

    @unittest.skipIf(OverlayServer is None, "WebSocket/SUMO runtime dependencies are not installed")
    async def test_websocket_submission_returns_requirement_id_without_initializing_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            server = OverlayServer(
                SimpleNamespace(performance=None), "127.0.0.1", 0,
                requirement_repository=RequirementRepository(directory),
            )
            server.client = CapturingClient()
            await server._handle_message(json.dumps({
                "action": "submit_requirement",
                "request_id": "submit-1",
                "requirement": requirement_payload(),
            }))
            response = server.client.messages[-1]
            self.assertEqual("requirement_accepted", response["type"])
            self.assertEqual("submit-1", response["request_id"])
            self.assertRegex(response["requirement_id"], r"^req-[0-9a-f]{32}$")

    @unittest.skipIf(OverlayServer is None, "WebSocket/SUMO runtime dependencies are not installed")
    async def test_nonuniform_population_is_saved_exactly_as_submitted(self):
        payload = requirement_payload()
        payload["population"]["distributions"]["gender"][0]["percent"] = 47
        payload["population"]["distributions"]["gender"][1]["percent"] = 53
        with tempfile.TemporaryDirectory() as directory:
            repository = RequirementRepository(directory)
            server = OverlayServer(SimpleNamespace(performance=None), "127.0.0.1", 0, requirement_repository=repository)
            server.client = CapturingClient()
            await server._handle_message(json.dumps({
                "action": "submit_requirement", "request_id": "advised-1", "requirement": payload,
            }))
            response = server.client.messages[-1]
            self.assertEqual("requirement_accepted", response["type"])
            loaded = repository.load(response["requirement_id"])
            self.assertEqual(payload, loaded["requirement"])
            self.assertEqual({"total", "distributions"}, set(loaded["requirement"]["population"]))

    def test_profile_distribution_uses_exact_integer_allocation(self):
        payload = requirement_payload()
        sampler = PopulationProfileSampler(
            seed=7,
            distributions=payload["population"]["distributions"],
        )
        person_ids = [f"p-{index}" for index in range(10)]
        sampler.prepare_population(person_ids)
        profiles = [sampler.sample_profile(person_id) for person_id in person_ids]
        self.assertEqual(3, sum(profile.crowd_role == "commuter" for profile in profiles))
        self.assertEqual(5, sum(profile.gender == "female" for profile in profiles))
        self.assertEqual(6, sum(profile.origin == "本地居民" for profile in profiles))

    def test_runtime_requirement_diagnostics_returns_frontend_display_fields(self):
        payload = requirement_payload()
        record = {
            "requirement_id": "req-0123456789abcdef0123456789abcdef",
            "schema_version": 1,
            "fingerprint": "sha256:test",
            "requirement": payload,
            "capabilities": {"location": "supported"},
        }
        diagnostics = requirement_runtime_summary(record)
        self.assertEqual("人民英雄纪念塔", diagnostics["spatial_scope"]["name"])
        self.assertEqual(10, diagnostics["population"]["total"])
        self.assertEqual("文化娱乐活动", diagnostics["scenario"]["event_category"]["label"])


if __name__ == "__main__":
    unittest.main()
