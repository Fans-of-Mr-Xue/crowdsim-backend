"""Cross-phase merge contracts; upstream models and databases are mocked."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from data_service.api import create_app
from data_service.api.models import register_model_routes, selected_request_model
from data_service.api.model_gateway import ModelGateway, register_model_gateway_routes
from data_service.connectors.models import ModelGateway as ProviderGateway
from tests.test_model_gateway import FakeSession, ModelGatewayHttpTests


class ComposedGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.main_session = FakeSession()
        self.main = ModelGateway(session=self.main_session, api_key='')
        self.providers = ProviderGateway()
        self.providers.open = AsyncMock()
        self.providers.session = FakeSession()
        self.providers.session.chat_response.json = AsyncMock(return_value={
            'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}],
        })
        self.providers.close = AsyncMock()
        self.providers.status = AsyncMock(return_value=[
            {'id': 'GLM-5.3-Flash-8800', 'port': 8800},
            {'id': 'GLM-5.3-Flash', 'port': 8899},
            {'id': 'Qwen3.8-Flash-Next', 'port': 8800},
            {'id': 'DiffusionGemma', 'port': 8500},
        ])
        app = web.Application()
        register_model_gateway_routes(app, gateway=self.main, include_model_routes=False)
        register_model_routes(app, main_gateway=self.main, gateway=self.providers)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_shared_aliases_keep_main_upstream_and_generation_options(self):
        for prefix in ('/api/crowdSim', '/crowdSim'):
            for name in ('GLM-5.3-Flash', 'GLM-5.3-Flash-8800'):
                response = await self.client.post(prefix + '/models/' + name + '/chat/completions', json={
                    'messages': [{'role': 'user', 'content': 'hello'}], 'reasoning_budget_tokens': 512,
                })
                self.assertEqual(response.status, 200, await response.text())
                self.assertEqual(self.main_session.calls[-1][1], 'http://127.0.0.1:8800/v1/chat/completions')
                self.assertEqual(self.main_session.calls[-1][2]['json']['reasoning_budget_tokens'], 512)
        self.assertEqual(self.providers.session.calls, [])

    async def test_explicit_tabby_and_legacy_pre_path_reach_original_provider(self):
        for path in ('/pre/models/GLM-5.3-Flash-Tabby', '/pre/models/GLM-5.3-Flash'):
            response = await self.client.post('/api/crowdSim' + path + '/chat/completions', json={
                'messages': [{'role': 'user', 'content': 'hello'}],
            })
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual(self.providers.session.calls[-1][1], self.providers.providers['GLM-5.3-Flash']['base_url'] + '/chat/completions')
        self.assertEqual(self.main_session.calls, [])

    async def test_stateless_contract_and_unambiguous_provider_status(self):
        response = await self.client.post('/api/crowdSim/llm/chat', json={'prompt': 'hello'})
        self.assertEqual(response.status, 200, await response.text())
        self.assertTrue((await response.json())['success'])
        response = await self.client.get('/api/crowdSim/models/status')
        providers = {item['id']: item['port'] for item in (await response.json())['data']}
        self.assertEqual(providers, {'GLM-5.3-Flash': 8800})
        self.providers.status.assert_not_awaited()
        response = await self.client.get('/api/crowdSim/pre/models/status')
        providers = {item['id']: item['port'] for item in (await response.json())['data']}
        self.assertEqual(providers, {'GLM-5.3-Flash-8800': 8800, 'GLM-5.3-Flash': 8899,
                                    'Qwen3.8-Flash-Next': 8800, 'DiffusionGemma': 8500})


class ApplicationContracts(unittest.TestCase):
    def test_application_has_unique_routes_and_both_phase_apis(self):
        enable_gridfs_integration()
        app = create_app(SimpleNamespace(db=mongomock.MongoClient().db, close=Mock()))
        routes = [(r.method, r.resource.canonical) for r in app.router.routes()]
        self.assertEqual(len(routes), len(set(routes)))
        for suffix in ('/models/{model_id}/chat/completions', '/llm/chat'):
            self.assertIn(('POST', '/api/crowdSim' + suffix), routes)
        self.assertTrue(any('/pre-assessments' in path for _, path in routes))
        self.assertTrue(any('/domain-portrait' in path for _, path in routes))

    def test_business_header_agrees_with_shared_menu(self):
        for public, internal in [('GLM-5.3-Flash', 'GLM-5.3-Flash-8800'),
                                 ('GLM-5.3-Flash-8800', 'GLM-5.3-Flash-8800'),
                                 ('GLM-5.3-Flash-Tabby', 'GLM-5.3-Flash')]:
            request = make_mocked_request('GET', '/', headers={'X-CrowdSim-Model': public})
            self.assertEqual(selected_request_model(request), internal)

    def test_provider_copy_preserves_generation_policy_and_session(self):
        from data_service.connectors.llm import JsonModelClient
        client = JsonModelClient()
        client.session = object()
        for name in ('GLM-5.3-Flash-8800', 'GLM-5.3-Flash', 'Qwen3.8-Flash-Next'):
            selected = client.for_provider(name)
            self.assertIs(selected.session, client.session)
            self.assertLessEqual(selected.max_output_tokens, 3000)
            self.assertFalse(selected.extra['chat_template_kwargs']['enable_thinking'])
            self.assertLessEqual(selected.extra['reasoning_budget_tokens'], 512)


class RemoteContractsAfterComposition(ModelGatewayHttpTests):
    """Run every remote gateway contract against the merged route composition."""
    async def asyncSetUp(self):
        self.session = FakeSession()
        self.gateway = ModelGateway(session=self.session, api_key='secret')
        providers = Mock(open=AsyncMock(), close=AsyncMock())
        app = web.Application()
        register_model_gateway_routes(app, gateway=self.gateway, include_model_routes=False)
        register_model_routes(app, main_gateway=self.gateway, gateway=providers)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
