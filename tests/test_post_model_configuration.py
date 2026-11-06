"""Post-review model requests and health checks must use the same configuration."""
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

from dialogue_service.post_agent import _model_reply
from postanalysis_api.services.application import PostAnalysisApp


class PostModelConfigurationTests(unittest.TestCase):
    def test_agent_defaults_to_glm_and_rejects_unknown_model(self):
        payload = {"choices": [{"message": {"content": '{"assistant_message":"ready"}'}}]}
        with patch.dict("os.environ", {}, clear=True), patch("dialogue_service.post_agent.build_opener") as opener:
            opener.return_value.open.return_value = BytesIO(json.dumps(payload).encode())
            _model_reply([], "GLM-5.3-Flash", 5000)
            request = opener.return_value.open.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual("GLM-5.3-Flash", body["model"])
            self.assertEqual(3000, body["max_tokens"])
            self.assertEqual(512, body["reasoning_budget_tokens"])
            with self.assertRaises(ValueError):
                _model_reply([], "unknown", 1200)
            self.assertEqual(1, opener.return_value.open.call_count)

    def test_selected_models_use_their_own_endpoint_model_and_key(self):
        from data_service.connectors.models import model_configs
        for identifier in ("Qwen3.8-Flash-Next", "DiffusionGemma"):
            with self.subTest(model=identifier), patch.dict("os.environ", {}, clear=True), patch("dialogue_service.post_agent.build_opener") as opener:
                payload = {"choices": [{"message": {"content": '{"assistant_message":"ready"}'}}]}
                opener.return_value.open.return_value = BytesIO(json.dumps(payload).encode())
                _model_reply([], identifier, 1200)
                provider = model_configs()[identifier]
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(provider["base_url"] + "/chat/completions", request.full_url)
                self.assertEqual(provider["model"], json.loads(request.data)["model"])

    def test_discovered_model_is_checked_and_sent_with_actual_upstream_name(self):
        payload = {'choices': [{'message': {'content': '{"assistant_message":"ready"}'}}]}
        with patch.dict('os.environ', {}, clear=True), patch('dialogue_service.post_agent.build_opener') as opener:
            opener.return_value.open.side_effect = [
                BytesIO(json.dumps({'data': [{'id': 'Qwen3.6-35B-A3B'}]}).encode()),
                BytesIO(json.dumps(payload).encode())]
            _model_reply([], 'DiffusionGemma::Qwen3.6-35B-A3B', 1200)
            requests = opener.return_value.open.call_args_list
            self.assertEqual('http://127.0.0.1:8500/v1/models', requests[0].args[0].full_url)
            self.assertEqual('Qwen3.6-35B-A3B', json.loads(requests[1].args[0].data)['model'])

    def test_health_and_agent_share_generic_and_post_specific_configuration(self):
        for overrides, expected_model, expected_token in (
            ({}, "GLM-5.3-Flash", "generic-key"),
            ({"CROWDSIM_LLM_MODEL": "GLM-5.3-Flash-exl3-4.05bpw"}, "GLM-5.3-Flash-exl3-4.05bpw", "generic-key"),
            ({"CROWDSIM_POST_LLM_MODEL": "GLM-5.3-Flash-8800", "CROWDSIM_POST_LLM_API_KEY": "post-key"}, "GLM-5.3-Flash-8800", "post-key"),
        ):
            environment = {"CROWDSIM_LLM_BASE_URL": "http://127.0.0.1:18800/v1/",
                           "CROWDSIM_MODEL_API_KEY": "generic-key", **overrides}
            with self.subTest(overrides=overrides), patch.dict("os.environ", environment, clear=True):
                with patch("postanalysis_api.services.application.urlopen") as health:
                    health.return_value = BytesIO(json.dumps({"data": [{"id": expected_model}]}).encode())
                    # The health method does not require a database connection.
                    result = PostAnalysisApp.model_connection(None)
                    self.assertEqual("ready", result["status"])
                    self.assertEqual(expected_model, result["model"])
                    self.assertEqual("GLM-5.3 Flash", result["displayName"])
                    request = health.call_args.args[0]
                    self.assertEqual("http://127.0.0.1:18800/v1/models", request.full_url)
                    self.assertEqual("Bearer " + expected_token, request.get_header("Authorization"))
                with patch("dialogue_service.post_agent.build_opener") as opener:
                    payload = {"choices": [{"message": {"content": '{"assistant_message":"ready"}'}}]}
                    opener.return_value.open.return_value = BytesIO(json.dumps(payload).encode())
                    _model_reply([], "GLM-5.3-Flash", 1200)
                    request = opener.return_value.open.call_args.args[0]
                    self.assertEqual(expected_model, json.loads(request.data)["model"])
                    self.assertEqual("http://127.0.0.1:18800/v1/chat/completions", request.full_url)
                    self.assertEqual("Bearer " + expected_token, request.get_header("Authorization"))

    def test_unreachable_health_still_reports_glm(self):
        with patch.dict("os.environ", {}, clear=True), patch(
            "postanalysis_api.services.application.urlopen", side_effect=URLError("offline")
        ):
            result = PostAnalysisApp.model_connection(None)
        self.assertEqual("unreachable", result["status"])
        self.assertEqual("GLM-5.3-Flash", result["model"])
        self.assertEqual("GLM-5.3 Flash", result["displayName"])


if __name__ == "__main__":
    unittest.main()
