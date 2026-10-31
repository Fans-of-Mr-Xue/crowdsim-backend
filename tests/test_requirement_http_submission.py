"""Saving requirements must leave the sole runtime socket available."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.websocket_server import OverlayServer
from tests.test_requirement_submission import requirement_payload
from tests.test_requirement_location_v2 import located_requirement


class RequirementHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.runtime = SimpleNamespace(state="RUNNING")
        self.server = OverlayServer(self.runtime, "127.0.0.1", 0,
                                    requirement_repository=RequirementRepository(self.root))
        self.server._send = AsyncMock(side_effect=AssertionError("HTTP must not use the runtime socket"))

    async def submit(self, payload):
        request = SimpleNamespace(json=AsyncMock(return_value={"requirement": payload}))
        response = await self.server._requirement_http_handler(request)
        return response.status, json.loads(response.text)

    async def test_save_leaves_socket_free_and_record_available_to_runtime(self):
        status, result = await self.submit(requirement_payload())
        self.assertEqual(201, status)
        self.assertEqual("requirement_accepted", result["type"])
        record = self.server.requirements.load(result["requirement_id"])
        self.assertEqual(10, record["requirement"]["population"]["total"])
        self.assertIsNone(self.server.client)
        self.assertIsNone(self.server.task)
        self.server._send.assert_not_called()

    async def test_save_does_not_pause_or_disconnect_an_existing_runtime_client(self):
        existing_client = object()
        self.server.client = existing_client
        status, result = await self.submit(requirement_payload())
        self.assertEqual(201, status)
        self.assertIs(existing_client, self.server.client)
        self.assertEqual("RUNNING", self.runtime.state)
        self.server._send.assert_not_called()

    async def test_confirmed_v2_snapshot_retries_keep_the_same_requirement_id(self):
        first = await self.submit(located_requirement(True))
        second = await self.submit(located_requirement(True))
        self.assertEqual(201, first[0])
        self.assertEqual(first[1]["requirement_id"], second[1]["requirement_id"])
        self.assertEqual(1, len(list(self.root.glob("*.json"))))

    async def test_invalid_parameters_do_not_create_a_record(self):
        raw = requirement_payload()
        raw["population"]["total"] = -1
        status, result = await self.submit(raw)
        self.assertEqual(400, status)
        self.assertEqual("invalid_requirement", result["code"])
        self.assertEqual([], list(self.root.glob("*.json")))

    async def test_non_object_or_malformed_json_is_rejected(self):
        for value in ([], None):
            with self.subTest(value=value):
                response = await self.server._requirement_http_handler(SimpleNamespace(json=AsyncMock(return_value=value)))
                self.assertEqual(400, response.status)
        response = await self.server._requirement_http_handler(SimpleNamespace(json=AsyncMock(
            side_effect=json.JSONDecodeError("invalid", "", 0))))
        self.assertEqual(400, response.status)
        self.assertEqual("invalid_json", json.loads(response.text)["code"])

    async def test_storage_errors_are_not_reported_as_accepted(self):
        with patch.object(self.server.requirements, "create", side_effect=OSError("disk full")):
            status, result = await self.submit(requirement_payload())
        self.assertEqual(500, status)
        self.assertEqual("requirement_storage_failed", result["code"])
