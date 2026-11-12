import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from crowdsim.during_models import GLM, DuringModelGateway, register_during_model_routes
from data_service.api.model_gateway import GatewayInputError


class DuringModelsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.glm = SimpleNamespace(open=AsyncMock(), close=AsyncMock(),
            status=AsyncMock(return_value={"id": GLM, "available": True}),
            chat=AsyncMock(return_value={"model": GLM}))
        self.gateway = DuringModelGateway(glm=self.glm)

    async def test_glm_preserves_the_original_gateway_payload(self):
        body = {"messages": [{"role": "user", "content": "synthetic"}], "max_tokens": 3000,
                "chat_template_kwargs": {"enable_thinking": False}, "reasoning_budget_tokens": 512,
                "response_format": {"type": "json_object"}}
        original = copy.deepcopy(body)
        self.assertEqual({"model": GLM}, await self.gateway.chat(GLM, body))
        self.glm.chat.assert_awaited_once_with(body, GLM)
        self.assertEqual(original, body)
        await self.gateway.open()
        await self.gateway.close()
        self.glm.open.assert_awaited_once()
        self.glm.close.assert_awaited_once()

    async def test_removed_and_unknown_providers_do_not_contact_upstream(self):
        for identifier in ("deepseek-flash", "unknown", None):
            with self.subTest(identifier=identifier), self.assertRaises(GatewayInputError):
                await self.gateway.chat(identifier, {})
        self.glm.chat.assert_not_called()

    async def test_status_lists_only_glm_and_reports_an_unreachable_service(self):
        self.assertEqual([{"id": GLM, "available": True}], await self.gateway.status())
        for error in (TimeoutError(), aiohttp.ClientConnectionError()):
            self.glm.status.side_effect = error
            status = await self.gateway.status()
            self.assertEqual([GLM], [item["id"] for item in status])
            self.assertFalse(status[0]["available"])

    async def test_http_health_and_protocol_need_no_database(self):
        app = web.Application()
        register_during_model_routes(app, gateway=self.gateway)
        with patch("pymongo.MongoClient", side_effect=AssertionError("database forbidden")):
            async with TestClient(TestServer(app)) as client:
                health = await (await client.get("/api/crowdSim/during/health")).json()
                self.assertFalse(health["databaseRequired"])
                status = await (await client.get("/api/crowdSim/during/models/status")).json()
                self.assertEqual([GLM], [m["id"] for m in status["data"]])
                response = await client.post(f"/api/crowdSim/during/models/{GLM}/chat/completions",
                    json={"messages": [{"role": "user", "content": "synthetic"}]})
                self.assertEqual(200, response.status)
                removed = await client.post("/api/crowdSim/during/models/deepseek-flash/chat/completions", json={})
                self.assertEqual(400, removed.status)
                self.assertEqual(404, (await client.get("/api/v1/post/capabilities")).status)

    async def test_legacy_reset_restores_shared_defaults(self):
        from crowdsim_overlay_server import parse_args, build_runtime
        from crowdsim.during_server import DuringOverlayServer
        runtime = build_runtime(parse_args(["--scenario", "hotspot"]))
        original = (runtime.decision_engine.llm.base_url, runtime.decision_engine.llm.model)
        try:
            runtime.configure_during_llm(GLM)
            server = DuringOverlayServer(runtime, "127.0.0.1", 8765)
            server._prepare = AsyncMock()
            server._send_init = AsyncMock()
            await server._configure({}, "legacy-run", replace_run=True)
            self.assertIsNone(runtime.during_llm_model)
            self.assertEqual(original, (runtime.decision_engine.llm.base_url, runtime.decision_engine.llm.model))
        finally:
            runtime.close()


class DuringIsolationTests(unittest.TestCase):
    def test_observation_reset_forwards_only_the_frozen_model_id(self):
        from crowdsim.experiments.orchestrator import ExperimentOrchestrator
        binding = {"requirementId": "req-test", "modelId": "asm-test", "modelFingerprint": "sha256:test", "llmModel": GLM}
        payload = ExperimentOrchestrator._prepare_reset_payload(None, {"population": 10, "observationBinding": binding}, 42)
        self.assertEqual(GLM, payload["during_llm_model"])
        self.assertNotIn("api_key", str(payload))

    def test_runtime_reset_retains_glm_without_starting_sumo(self):
        from crowdsim_overlay_server import parse_args, build_runtime
        from crowdsim.core.simulation_runtime import SimulationRuntime
        with patch.object(SimulationRuntime, "close"), patch.object(SimulationRuntime, "initialize", return_value="initialized"):
            runtime = build_runtime(parse_args(["--scenario", "hotspot"]))
            runtime.configure_during_llm(GLM)
            self.assertEqual("initialized", runtime.reset(seed=42))
            self.assertEqual(GLM, runtime.during_llm_model)
            self.assertEqual(GLM, runtime.decision_engine.llm.model)

    def test_during_check_does_not_start_services_or_tunnels(self):
        import start_during
        with patch("start_during.check_service_dependencies"), patch("start_during.check_sumo_version"), \
             patch("crowdsim.infrastructure.sumo_adapter.discover_sumo_binary", return_value="sumo"), \
             patch("crowdsim_overlay_server.resolve_service_scenario"), \
             patch("start_during.Supervisor") as supervisor, \
             patch("start_backend.ensure_tunnels", side_effect=AssertionError("SSH forbidden")), \
             patch("pymongo.MongoClient", side_effect=AssertionError("MongoDB forbidden")):
            self.assertEqual(0, start_during.main(["--check"]))
            supervisor.return_value.start_service.assert_not_called()

    def test_independent_simulator_has_no_post_routes_or_database_construction(self):
        from crowdsim.during_server import DuringOverlayServer
        with patch("crowdsim.infrastructure.websocket_server.PostAnalysisApp", side_effect=AssertionError("post forbidden")):
            server = DuringOverlayServer(SimpleNamespace(), "127.0.0.1", 8765)
            paths = [r.resource.canonical for r in server.create_app().router.routes()]
        self.assertIn("/api/v1/during/health", paths)
        self.assertFalse(any("/post" in p for p in paths))
        self.assertIsNone(server.post_app)

    def test_runtime_glm_binding_is_instance_local_and_restores_shared_settings(self):
        from crowdsim.core.simulation_runtime import SimulationRuntime, RuntimeState
        from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
        env = {"CROWDSIM_LLM_MODEL": "shared-model", "CROWDSIM_LLM_BASE_URL": "http://shared.invalid/v1"}
        with patch.dict(os.environ, env):
            first = SimpleNamespace(state=RuntimeState.CREATED, during_llm_model=None,
                decision_engine=SimpleNamespace(llm=LLMDrivenDecisionModel(), model="shared-model"))
            second = LLMDrivenDecisionModel()
            original = (second.base_url, second.model, second.api_key)
            SimulationRuntime.configure_during_llm(first, GLM)
            self.assertEqual(GLM, first.decision_engine.model)
            self.assertEqual("http://127.0.0.1:8800/v1", first.decision_engine.llm.base_url)
            self.assertEqual(original, (second.base_url, second.model, second.api_key))
            self.assertEqual(env["CROWDSIM_LLM_MODEL"], os.environ["CROWDSIM_LLM_MODEL"])
            with self.assertRaises(ValueError):
                SimulationRuntime.configure_during_llm(first, "deepseek-flash")
            SimulationRuntime.configure_during_llm(first, None)
            self.assertEqual(original, (first.decision_engine.llm.base_url, first.decision_engine.llm.model, first.decision_engine.llm.api_key))
