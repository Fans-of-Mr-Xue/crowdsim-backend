import copy
import os
import socket
import ssl
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from crowdsim.during_models import DeepSeekSettings, DuringModelGateway, register_during_model_routes
from data_service.api.model_gateway import GatewayInputError, GatewayUpstreamError


class Response:
    status = 200
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False
    async def json(self):
        return {"model": "deepseek-flash", "choices": [{"message": {"content": '{"reply":"ok"}'}, "finish_reason": "stop"}]}


class DuringModelsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = DeepSeekSettings(True, "https://api.deepseek.com", "deepseek-flash", "test-secret")
        self.glm = SimpleNamespace(open=AsyncMock(), close=AsyncMock(), status=AsyncMock(return_value={"id": "GLM-5.3-Flash", "available": True}), chat=AsyncMock(return_value={"model": "original-glm"}))
        self.session = SimpleNamespace(post=Mock(return_value=Response()))
        self.gateway = DuringModelGateway(settings=self.settings, glm=self.glm, session=self.session)

    async def test_deepseek_converts_only_its_request_and_preserves_provenance(self):
        body = {"messages": [{"role": "user", "content": "synthetic"}], "max_tokens": 300,
                "chat_template_kwargs": {"enable_thinking": False}, "reasoning_budget_tokens": 512,
                "response_format": {"type": "json_object"}}
        original = copy.deepcopy(body)
        result = await self.gateway.chat("deepseek-flash", body)
        self.assertEqual("deepseek-flash", result["model"])
        args, kwargs = self.session.post.call_args
        self.assertEqual("https://api.deepseek.com/chat/completions", args[0])
        self.assertEqual({"type": "disabled"}, kwargs["json"]["thinking"])
        self.assertNotIn("chat_template_kwargs", kwargs["json"])
        self.assertNotIn("reasoning_budget_tokens", kwargs["json"])
        self.assertEqual(original, body)
        self.assertNotIn("test-secret", str(result))
        self.assertNotIn("proxy", kwargs)
        self.glm.chat.assert_not_called()

    async def test_deepseek_proxy_is_explicit_and_does_not_affect_glm(self):
        self.gateway.settings = replace(self.settings, proxy_url="http://127.0.0.1:1082")
        body = {"messages": [{"role": "user", "content": "synthetic"}]}
        await self.gateway.chat("deepseek-flash", body)
        self.assertEqual("http://127.0.0.1:1082", self.session.post.call_args.kwargs["proxy"])
        self.session.post.reset_mock()
        await self.gateway.chat("GLM-5.3-Flash", body)
        self.glm.chat.assert_awaited_once_with(body, "GLM-5.3-Flash")
        self.session.post.assert_not_called()

    async def test_invalid_proxy_is_rejected_without_contact_or_credentials_in_errors(self):
        for proxy in ("socks5://test-secret@localhost:1082", "http://", "http://localhost:0",
                      "http://localhost:99999", "http://localhost:bad", "http://localhost/private"):
            with self.subTest(proxy=proxy):
                self.gateway.settings = replace(self.settings, proxy_url=proxy)
                with self.assertRaises(GatewayUpstreamError) as caught:
                    await self.gateway.chat("deepseek-flash", {"messages": [{"role": "user", "content": "synthetic"}]})
                self.assertIn("代理配置无效", str(caught.exception))
                self.assertNotIn("test-secret", str(caught.exception))
                self.session.post.assert_not_called()

    async def test_transport_errors_are_distinguished_and_sanitized(self):
        connection = SimpleNamespace(host="api.deepseek.com", port=443, ssl=True)
        cases = (
            (TimeoutError("test-secret"), 504, "超时"),
            (aiohttp.ClientProxyConnectionError(connection, OSError("test-secret")), 503, "代理连接失败"),
            (aiohttp.ClientHttpProxyError(None, (), status=407, message="test-secret"), 503, "代理连接失败"),
            (aiohttp.ClientConnectorCertificateError(connection, ssl.SSLCertVerificationError(1, "test-secret")), 503, "TLS"),
            (aiohttp.ClientConnectorError(connection, socket.gaierror(-2, "test-secret")), 503, "DNS"),
            (aiohttp.ClientConnectorError(connection, ConnectionResetError("test-secret")), 503, "连接失败"),
            (aiohttp.ClientConnectionError("test-secret"), 503, "连接被中断"),
            (ValueError("test-secret"), 502, "格式无效"),
        )
        for error, status, message in cases:
            with self.subTest(error=type(error).__name__):
                self.session.post.side_effect = error
                with self.assertRaises(GatewayUpstreamError) as caught:
                    await self.gateway.chat("deepseek-flash", {"messages": [{"role": "user", "content": "synthetic"}]})
                self.assertEqual(status, caught.exception.status)
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("test-secret", str(caught.exception))

    async def test_local_proxy_env_is_loaded_without_inheriting_global_proxy(self):
        env = {"CROWDSIM_DURING_DEEPSEEK_ENABLED": "true", "CROWDSIM_DURING_DEEPSEEK_API_KEY": "test-secret",
               "CROWDSIM_DURING_DEEPSEEK_PROXY": " http://127.0.0.1:1082 ",
               "HTTPS_PROXY": "http://test-secret@unrelated:8888"}
        with patch.dict(os.environ, env, clear=True), patch("crowdsim.during_models.load_database_env"):
            settings = DeepSeekSettings.load()
            self.assertEqual("http://127.0.0.1:1082", settings.proxy_url)
            self.assertNotIn("test-secret", repr(settings))
            os.environ.pop("CROWDSIM_DURING_DEEPSEEK_PROXY")
            self.assertEqual("", DeepSeekSettings.load().proxy_url)

    async def test_glm_uses_original_gateway_without_deepseek_transformation(self):
        body = {"messages": [{"role": "user", "content": "synthetic"}], "reasoning_budget_tokens": 512}
        await self.gateway.chat("GLM-5.3-Flash", body)
        self.glm.chat.assert_awaited_once_with(body, "GLM-5.3-Flash")
        self.session.post.assert_not_called()

    async def test_disabled_unknown_and_invalid_requests_do_not_contact_upstream(self):
        self.gateway.settings = DeepSeekSettings(False, "https://api.deepseek.com", "deepseek-flash", "")
        with self.assertRaises(GatewayUpstreamError):
            await self.gateway.chat("deepseek-flash", {})
        with self.assertRaises(GatewayInputError):
            await self.gateway.chat("unknown", {})
        self.gateway.settings = self.settings
        with self.assertRaises(GatewayInputError):
            await self.gateway.chat("deepseek-flash", {"messages": [], "api_key": "not-allowed"})
        self.session.post.assert_not_called()

    async def test_http_health_and_protocol_need_no_database(self):
        app = web.Application()
        register_during_model_routes(app, gateway=self.gateway)
        with patch("pymongo.MongoClient", side_effect=AssertionError("database forbidden")):
            async with TestClient(TestServer(app)) as client:
                health = await (await client.get("/api/crowdSim/during/health")).json()
                self.assertFalse(health["databaseRequired"])
                status = await (await client.get("/api/crowdSim/during/models/status")).json()
                self.assertEqual(["GLM-5.3-Flash", "deepseek-flash"], [m["id"] for m in status["data"]])
                response = await client.post("/api/crowdSim/during/models/deepseek-flash/chat/completions", json={"messages": [{"role": "user", "content": "synthetic"}]})
                self.assertEqual(200, response.status)
                self.assertEqual(404, (await client.get("/api/v1/post/capabilities")).status)

    async def test_legacy_reset_does_not_inherit_the_previous_during_provider(self):
        from crowdsim_overlay_server import parse_args, build_runtime
        from crowdsim.during_server import DuringOverlayServer
        runtime = build_runtime(parse_args(["--scenario", "hotspot"]))
        original = (runtime.decision_engine.llm.base_url, runtime.decision_engine.llm.model)
        try:
            with patch("crowdsim.during_models.DeepSeekSettings.load", return_value=self.settings):
                runtime.configure_during_llm("deepseek-flash")
            server = DuringOverlayServer(runtime, "127.0.0.1", 8765)
            server._prepare = AsyncMock()  # Keep this regression independent of SUMO.
            server._send_init = AsyncMock()
            await server._configure({}, "legacy-run", replace_run=True)
            self.assertIsNone(runtime.during_llm_model)
            self.assertEqual(original, (runtime.decision_engine.llm.base_url, runtime.decision_engine.llm.model))
        finally:
            runtime.close()


class DuringIsolationTests(unittest.TestCase):
    def test_observation_reset_forwards_only_the_frozen_model_id(self):
        from crowdsim.experiments.orchestrator import ExperimentOrchestrator
        binding = {"requirementId": "req-test", "modelId": "asm-test", "modelFingerprint": "sha256:test", "llmModel": "deepseek-flash"}
        payload = ExperimentOrchestrator._prepare_reset_payload(None, {"population": 10, "observationBinding": binding}, 42)
        self.assertEqual("deepseek-flash", payload["during_llm_model"])
        self.assertNotIn("api_key", str(payload))

    def test_runtime_reset_retains_its_frozen_provider_without_starting_sumo(self):
        from crowdsim_overlay_server import parse_args, build_runtime
        from crowdsim.core.simulation_runtime import SimulationRuntime
        with patch("crowdsim.during_models.DeepSeekSettings.load", return_value=DeepSeekSettings(True, "https://api.deepseek.com", "deepseek-flash", "test-only")), \
             patch.object(SimulationRuntime, "close"), patch.object(SimulationRuntime, "initialize", return_value="initialized"):
            runtime = build_runtime(parse_args(["--scenario", "hotspot"]))
            runtime.configure_during_llm("deepseek-flash")
            self.assertEqual("initialized", runtime.reset(seed=42))
            self.assertEqual("deepseek-flash", runtime.during_llm_model)
            self.assertEqual("deepseek-flash", runtime.decision_engine.llm.model)

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

    def test_runtime_provider_binding_is_instance_local_and_restores_glm_defaults(self):
        from crowdsim.core.simulation_runtime import SimulationRuntime, RuntimeState
        from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
        first = SimpleNamespace(state=RuntimeState.CREATED, during_llm_model=None, decision_engine=SimpleNamespace(llm=LLMDrivenDecisionModel(), model="original"))
        second = LLMDrivenDecisionModel()
        original = (second.base_url, second.model, second.api_key)
        with patch("crowdsim.during_models.DeepSeekSettings.load", return_value=DeepSeekSettings(True, "https://api.deepseek.com", "deepseek-flash", "test-only")):
            SimulationRuntime.configure_during_llm(first, "deepseek-flash")
        self.assertEqual("deepseek-flash", first.decision_engine.model)
        self.assertEqual({"thinking": {"type": "disabled"}}, first.decision_engine.llm.extra_body)
        self.assertEqual(original, (second.base_url, second.model, second.api_key))
        SimulationRuntime.configure_during_llm(first, "GLM-5.3-Flash")
        self.assertEqual(original, (first.decision_engine.llm.base_url, first.decision_engine.llm.model, first.decision_engine.llm.api_key))
