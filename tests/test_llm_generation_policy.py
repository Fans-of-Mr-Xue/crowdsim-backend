"""Offline acceptance: inspect actual request bodies without HTTP listeners."""
import asyncio
from io import BytesIO
import json
import unittest
from unittest.mock import patch

from crowdsim.domain.llm_generation import generation_options
from data_service.connectors.llm import JsonModelClient, ModelError
from dialogue_service.post_agent import _model_reply, PostAgentUnavailable, respond


class GenerationPolicyTests(unittest.TestCase):
    def test_local_model_defaults_limits_and_nonlocal_provider_options(self):
        for model in ("GLM-5.3-Flash", "GLM-5.3-Flash-exl3-4.05bpw", "qwen3.8-flash-next"):
            options = generation_options(model, 5000)
            self.assertEqual(3000, options["max_tokens"])
            self.assertEqual(512, options["reasoning_budget_tokens"])
            self.assertEqual({"enable_thinking": False}, options["chat_template_kwargs"])
        extra = {"chat_template_kwargs": {"custom": "kept"}, "reasoning_budget_tokens": 128}
        before = json.dumps(extra)
        options = generation_options("GLM-5.3-Flash", 1800, extra)
        self.assertEqual(1800, options["max_tokens"])
        self.assertEqual(128, options["reasoning_budget_tokens"])
        self.assertEqual("kept", options["chat_template_kwargs"]["custom"])
        self.assertEqual(before, json.dumps(extra))
        self.assertEqual({"max_tokens": 300}, generation_options("other-provider", 300))
        for value in (-1, True, 1.5, "512"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                generation_options("GLM-5.3-Flash", 2048, {"reasoning_budget_tokens": value})

    def test_post_agent_serializes_limits_and_rejects_truncation(self):
        requests = []
        finish_reason = "stop"

        def open_request(request, **_):
            requests.append(json.loads(request.data))
            return BytesIO(json.dumps({"choices": [{"finish_reason": finish_reason, "message": {
                "content": '{"assistant_message":"等待用户核对。"}',
                "reasoning_content": "不得作为答案返回的内部推理",
            }}]}).encode())

        with patch.dict("os.environ", {"CROWDSIM_LLM_MODEL": "GLM-5.3-Flash"}, clear=True), patch(
            "dialogue_service.post_agent.build_opener"
        ) as opener:
            opener.return_value.open.side_effect = open_request
            reply = _model_reply([{"role": "user", "content": "核对方案"}], None, 5000)
            self.assertEqual("等待用户核对。", reply["assistant_message"])
            self.assertEqual(3000, requests[0]["max_tokens"])
            self.assertEqual(512, requests[0]["reasoning_budget_tokens"])
            self.assertEqual({"enable_thinking": False}, requests[0]["chat_template_kwargs"])
            finish_reason = "length"
            with self.assertRaisesRegex(PostAgentUnavailable, "截断"):
                _model_reply([], None, 2048)
            self.assertEqual(2, len(requests))

    def test_post_agent_caps_the_legacy_frontend_budget_before_completion(self):
        budgets = []

        def complete(messages, model, budget):
            budgets.append(budget)
            return {"assistant_message": "请核对目标。"}

        for budget in (None, 5000, 1800):
            payload = {"task": "intake", "messages": [{"role": "user", "content": "检查目标"}]}
            if budget is not None:
                payload["maxTokens"] = budget
            respond(payload, complete=complete)
        self.assertEqual([3000, 3000, 1800], budgets)

    def test_data_model_request_keeps_the_capped_budget_after_environment_options(self):
        requests = []

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def json(self):
                return {"choices": [{"finish_reason": finish_reason, "message": {"content": '{"rows":[]}'}}]}

        class Session:
            def post(self, url, **options):
                requests.append(options["json"])
                return Response()

        finish_reason = "stop"
        environment = {"CROWDSIM_LLM_MODEL": "GLM-5.3-Flash", "CROWDSIM_LLM_EXTRA_BODY": json.dumps({
            "max_tokens": 5000, "chat_template_kwargs": {"custom": "kept"},
        })}
        with patch.dict("os.environ", environment, clear=True), patch("data_service.connectors.llm.load_database_env"):
            model = JsonModelClient()
            model.session = Session()
            self.assertEqual({"rows": []}, asyncio.run(model.extract("规则", "资料")))
            self.assertEqual(3000, requests[0]["max_tokens"])
            self.assertEqual(3000, model.max_output_tokens)
            self.assertEqual(512, requests[0]["reasoning_budget_tokens"])
            self.assertEqual({"custom": "kept", "enable_thinking": False}, requests[0]["chat_template_kwargs"])
            finish_reason = "length"
            with self.assertRaisesRegex(ModelError, "截断"):
                asyncio.run(model.extract("规则", "资料"))


if __name__ == "__main__":
    unittest.main()
