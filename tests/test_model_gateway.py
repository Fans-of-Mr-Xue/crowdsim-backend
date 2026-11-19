from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from data_service.api.model_gateway import ModelGateway, register_model_gateway_routes


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status = status
        self.payload = payload
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        if self._text is not None:
            return self._text
        return json.dumps(self.payload)


class FakeSession:
    def __init__(self, *, model_payload=None, chat_response=None):
        self.model_payload = model_payload or {"data": [{"id": "GLM-5.3-Flash"}]}
        self.chat_response = chat_response or FakeResponse(payload={
            "model": "GLM-5.3-Flash-exl3-4.05bpw",
            "choices": [{"message": {"content": "连接正常"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 10},
        })
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(payload=self.model_payload)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.chat_response


class ModelGatewayHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeSession()
        self.gateway = ModelGateway(session=self.session, base_url="http://127.0.0.1:8800/v1", api_key="secret")
        app = web.Application()
        register_model_gateway_routes(app, gateway=self.gateway)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_status_reports_all_configured_models(self):
        response = await self.client.get("/api/crowdSim/models/status")
        self.assertEqual(200, response.status)
        status = (await response.json())["data"]
        self.assertEqual(["GLM-5.3-Flash", "Qwen3.8-Flash-Next", "DiffusionGemma"], [item["id"] for item in status[:3]])
        self.assertEqual([8800, 8800, 8500], [item["port"] for item in status[:3]])
        self.assertEqual([True, False, False], [item["available"] for item in status[:3]])
        self.assertTrue(status[3]['available'])
        method, url, kwargs = self.session.calls[0]
        self.assertEqual(("GET", "http://127.0.0.1:8800/v1/models"), (method, url))
        self.assertEqual("Bearer secret", kwargs["headers"]["Authorization"])

    async def test_chat_forwards_allowlisted_payload_and_hides_upstream_model_name(self):
        body = {"messages": [{"role": "system", "content": "规则"}, {"role": "user", "content": "问题"}],
                "max_tokens": 128, "stream": False, "temperature": 0,
                "tools": [{"type": "function", "function": {"name": "lookup"}}], "tool_choice": "auto"}
        response = await self.client.post("/api/crowdSim/models/GLM-5.3-Flash/chat/completions", json=body)
        self.assertEqual(200, response.status)
        payload = await response.json()
        self.assertEqual("GLM-5.3-Flash", payload["model"])
        self.assertEqual("连接正常", payload["choices"][0]["message"]["content"])
        method, url, kwargs = self.session.calls[-1]
        self.assertEqual(("POST", "http://127.0.0.1:8800/v1/chat/completions"), (method, url))
        self.assertEqual("GLM-5.3-Flash", kwargs["json"]["model"])
        self.assertEqual(body["messages"], kwargs["json"]["messages"])
        self.assertEqual(body["tools"], kwargs["json"]["tools"])
        self.assertNotIn("secret", json.dumps(payload))

    async def test_business_alias_maps_to_same_upstream_model(self):
        response = await self.client.post("/api/crowdSim/models/GLM-5.3-Flash-8800/chat/completions",
                                          json={"messages": [{"role": "user", "content": "问题"}]})
        self.assertEqual(200, response.status)
        self.assertEqual("GLM-5.3-Flash", self.session.calls[-1][2]["json"]["model"])

    async def test_unknown_fields_streaming_and_bad_messages_are_rejected_before_upstream(self):
        cases = (
            ("/api/crowdSim/models/unknown/chat/completions",
             {"messages": [{"role": "user", "content": "x"}]}),
            ("/api/crowdSim/models/GLM-5.3-Flash/chat/completions",
             {"messages": [{"role": "user", "content": "x"}], "stream": True}),
            ("/api/crowdSim/models/GLM-5.3-Flash/chat/completions",
             {"messages": [{"role": "invalid", "content": "x"}]}),
            ("/api/crowdSim/models/GLM-5.3-Flash/chat/completions",
             {"messages": [{"role": "user", "content": "x"}], "model": "evil"}),
        )
        for path, body in cases:
            before = len(self.session.calls)
            with self.subTest(path=path, body=body):
                response = await self.client.post(path, json=body)
                self.assertEqual(400, response.status)
                self.assertIn("message", await response.json())
                self.assertEqual(before, len(self.session.calls))

    async def test_each_model_uses_its_configured_endpoint_and_upstream_id(self):
        for identifier in ("GLM-5.3-Flash", "Qwen3.8-Flash-Next", "DiffusionGemma"):
            with self.subTest(identifier=identifier):
                response = await self.client.post(f"/api/crowdSim/models/{identifier}/chat/completions",
                    json={"messages": [{"role": "user", "content": "hello"}]})
                self.assertEqual(200, response.status)
                provider = self.gateway.providers[identifier]
                self.assertEqual(identifier, (await response.json())["model"])
                self.assertEqual(provider["base_url"] + "/chat/completions", self.session.calls[-1][1])
                self.assertEqual(provider["model"], self.session.calls[-1][2]["json"]["model"])

    async def test_discovers_actual_served_model_and_revalidates_it_before_chat(self):
        self.session.model_payload = {'data': [{'id': 'Qwen3.6-35B-A3B'}]}
        status = await self.gateway.statuses()
        discovered = next(item for item in status if item['id'] == 'DiffusionGemma::Qwen3.6-35B-A3B')
        self.assertTrue(discovered['available'])
        self.assertEqual(8500, discovered['port'])
        response = await self.client.post('/api/crowdSim/models/' + discovered['id'] + '/chat/completions',
            json={'messages': [{'role': 'user', 'content': 'hello'}]})
        self.assertEqual(200, response.status)
        self.assertEqual('http://127.0.0.1:8500/v1/chat/completions', self.session.calls[-1][1])
        self.assertEqual('Qwen3.6-35B-A3B', self.session.calls[-1][2]['json']['model'])
        self.session.model_payload = {'data': []}
        before = sum(call[0] == 'POST' for call in self.session.calls)
        response = await self.client.post('/api/crowdSim/models/' + discovered['id'] + '/chat/completions',
            json={'messages': [{'role': 'user', 'content': 'hello'}]})
        self.assertEqual(400, response.status)
        self.assertEqual(before, sum(call[0] == 'POST' for call in self.session.calls))

    async def test_stateless_route_preserves_during_contract(self):
        response = await self.client.post("/api/crowdSim/llm/chat", json={
            "prompt": "只输出 JSON", "model": "GLM-5.3-Flash", "maxTokens": 256,
            "thinkingDepth": "low", "timeoutMs": 120000, "responseFormat": "json_object",
        })
        self.assertEqual(200, response.status)
        payload = await response.json()
        self.assertTrue(payload["success"])
        self.assertEqual("连接正常", payload["data"]["text"])
        self.assertEqual("GLM-5.3-Flash", payload["data"]["model"])
        self.assertEqual([], payload["data"]["toolCalls"])
        self.assertEqual(256, self.session.calls[-1][2]["json"]["max_tokens"])
        self.assertEqual({"enable_thinking": False}, self.session.calls[-1][2]["json"]["chat_template_kwargs"])
        self.assertEqual({"type": "json_object"}, self.session.calls[-1][2]["json"]["response_format"])

    async def test_stateless_route_rejects_unknown_response_format_before_upstream(self):
        before = len(self.session.calls)
        response = await self.client.post("/api/crowdSim/llm/chat", json={
            "prompt": "问题", "model": "GLM-5.3-Flash", "responseFormat": "schema",
        })
        self.assertEqual(400, response.status)
        self.assertIn("responseFormat", (await response.json())["message"])
        self.assertEqual(before, len(self.session.calls))

    async def test_upstream_failure_is_sanitized(self):
        self.session.chat_response = FakeResponse(status=404, text="private upstream details")
        response = await self.client.post("/api/crowdSim/models/GLM-5.3-Flash/chat/completions",
                                          json={"messages": [{"role": "user", "content": "问题"}]})
        self.assertEqual(502, response.status)
        payload = await response.json()
        self.assertEqual("模型服务返回 HTTP 404", payload["message"])
        self.assertNotIn("private", json.dumps(payload))


class ModelGatewayStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_glm_is_unavailable(self):
        session = FakeSession(model_payload={"data": [{"id": "other"}]})
        gateway = ModelGateway(session=session, base_url="http://127.0.0.1:8800/v1", api_key="")
        status = await gateway.status()
        self.assertFalse(status["available"])
        self.assertIn("未加载", status["message"])


if __name__ == "__main__":
    unittest.main()
