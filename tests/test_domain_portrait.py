"""Formal model compatibility: legacy tasks, saved previews and common inputs."""
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from data_service.api.domain_portrait import register_domain_portrait_routes
from data_service.prediction.inputs import default_settings
from data_service.repositories.analysis_tasks import AnalysisTaskRepository
from data_service.services.domain_portrait import build_portrait, EDITABLE_SETTINGS


class DomainPortraitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        enable_gridfs_integration()
        self.db = mongomock.MongoClient().db
        self.repo = AnalysisTaskRepository(self.db)
        self.roads = json.loads((Path(__file__).resolve().parents[1] / 'data_service/resources/geography/shanghai-monument-sumo.geojson').read_text())
        self.payload = dict(title='纪念塔验证', time_window='2026-12-31 20:00 至 2027-01-01 00:30',
                            region={'mode': 'custom', 'name': '纪念塔', 'geometry': self.roads['boundary']},
                            population={'crowd': [{'label': '访客', 'value': 100}]}, components={})
        self.task = self.repo.save(self.payload)
        self.metadata = {'id': 'roads', 'source': {'environments': [{'environment_id': 'shanghai-monument-portrait-region', 'environment_revision': 6}]}}
        self.road_service = SimpleNamespace(task_context=AsyncMock(side_effect=lambda task: {**task, 'road_network': self.metadata}),
                                            snapshots=SimpleNamespace(read=Mock(return_value=self.roads)))
        app = web.Application()
        register_domain_portrait_routes(app, self.db, task_roads=self.road_service)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.url = f'/api/crowdSim/analysis-tasks/{self.task["id"]}/domain-portrait'

    async def asyncTearDown(self):
        await self.client.close()

    async def read(self):
        response = await self.client.get(self.url)
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())['data']

    async def test_legacy_read_has_full_evidence_and_does_not_write_or_revise_task(self):
        before = self.repo.get(self.task['id'])
        result = await self.read()
        self.assertEqual(result['portrait']['mode'], 'formal')
        self.assertEqual(len(result['portrait']['visualizations']['history']['records']), 4)
        self.assertEqual(len(result['portrait']['visualizations']['spatial_points']), 5)
        self.assertEqual(result['roads'], self.roads)
        self.assertEqual(self.repo.get(self.task['id']), before)
        self.assertEqual(self.db.analysis_task_components.count_documents({}), 0)

    async def test_preview_edits_survive_and_explicit_save_makes_common_inputs_available(self):
        scene = {'settings': {'initial_crowd': 150, 'dwell_minutes': 5}, 'population': {'crowd': [{'label': '自定义游客', 'value': 100}]}, 'observations': []}
        preview = {'mode': 'preview', 'scene': scene, 'candidates': [{'id': 'custom', 'title': '用户线索', 'retained': True}],
                   'evidence': [{'id': 'user_note', 'title': '用户记录', 'content': '保留原记录'}]}
        saved = self.repo.put_component(self.task['id'], 'domain_portrait', preview, 1)
        result = await self.read()
        portrait = result['portrait']
        self.assertEqual(portrait['scene']['settings']['initial_crowd'], 150)
        self.assertEqual(portrait['scene']['population'], scene['population'])
        self.assertTrue(next(c for c in portrait['candidates'] if c['id'] == 'custom')['retained'])
        settings = {key: portrait['scene']['settings'][key] for key in EDITABLE_SETTINGS}
        settings['initial_crowd'] = 175
        response = await self.client.put(self.url, json={'revision': saved['revision'], 'section': 'settings', 'data': settings})
        self.assertEqual(response.status, 200, await response.text())
        actual = self.repo.get(self.task['id'])
        self.assertEqual(default_settings(actual)['initial_crowd'], 175)
        self.assertEqual(actual['population'], scene['population'])
        self.assertEqual(actual['components']['domain_portrait']['mode'], 'formal')
        self.assertEqual((await self.read())['portrait']['scene']['settings']['initial_crowd'], 175)

    async def test_scene_save_is_atomic_and_rejects_invalid_or_stale_values(self):
        result = await self.read()
        settings = {key: result['portrait']['scene']['settings'][key] for key in EDITABLE_SETTINGS}
        response = await self.client.put(self.url, json={'revision': 1, 'section': 'settings', 'data': {**settings, 'dwell_minutes': -1}})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.repo.get(self.task['id'])['revision'], 1)
        response = await self.client.put(self.url, json={'revision': 1, 'section': 'settings', 'data': {**settings, 'initial_crowd': 321}})
        self.assertEqual(response.status, 200, await response.text())
        response = await self.client.put(self.url, json={'revision': 1, 'section': 'population', 'data': {}})
        self.assertEqual(response.status, 409)
        actual = self.repo.get(self.task['id'])
        self.assertEqual(actual['population'], self.task['population'])
        self.assertEqual(default_settings(actual)['initial_crowd'], 321)

    async def test_experiment_edits_are_visible_in_formal_model(self):
        portrait = build_portrait(self.task, self.roads, self.metadata)
        saved = self.repo.put_component(self.task['id'], 'domain_portrait', portrait, 1)
        self.repo.put_component(saved['id'], 'forecast_setup', {'initial_crowd': 234}, saved['revision'])
        result = await self.read()
        self.assertEqual(result['portrait']['scene']['settings']['initial_crowd'], 234)

    async def test_unrelated_site_does_not_inherit_monument_evidence_or_locations(self):
        task = copy.deepcopy(self.task)
        task['template_case'] = 'shanghai-bund-new-year'
        task['region']['geometry'] = {'type': 'Polygon', 'coordinates': [[[117, 39], [117.001, 39], [117.001, 39.001], [117, 39.001], [117, 39]]]}
        task['components']['domain_knowledge'] = {'annotations': [{'id': 'focus', 'title': '现场观察点', 'coordinates': [117.0005, 39.0005], 'status': 'pending'}]}
        portrait = build_portrait(task, {'features': []})
        self.assertNotIn('history', portrait['visualizations'])
        self.assertEqual([p['title'] for p in portrait['visualizations']['spatial_points']], ['现场观察点'])
        self.assertNotIn('official_monument', {e['id'] for e in portrait['evidence']})
        self.assertEqual(portrait['scene']['population'], task['population'])

    async def test_missing_task_returns_404(self):
        response = await self.client.get('/api/crowdSim/analysis-tasks/missing/domain-portrait')
        self.assertEqual(response.status, 404)

    async def test_news_is_evidence_only_and_never_auto_counted(self):
        self.db.news_articles.insert_many([
            {'title':'外滩活动举办公告', 'summary':'活动时间和局部人数待核对', 'url':'https://example.org/activity', 'publishedAt':'2026-01-02'},
            {'title':'外滩活动举办公告', 'summary':'未来发稿', 'url':'https://example.org/future', 'publishedAt':'2030-01-02'},
        ])
        before = self.repo.get(self.task['id'])
        portrait = (await self.read())['portrait']
        article = next(r for r in portrait['activities']['records'] if r['url']=='https://example.org/activity')
        self.assertIsNone(article['start_date'])
        self.assertIsNone(article['reported_attendance'])
        self.assertFalse(article['counted'])
        self.assertNotIn('https://example.org/future', [r['url'] for r in portrait['activities']['records']])
        self.assertEqual(self.repo.get(self.task['id']), before)
        self.assertEqual(len(portrait['model_inputs']['records']), 8)

    async def test_traffic_route_is_read_only_and_validates_scope(self):
        before = self.repo.get(self.task['id'])
        with patch.dict('os.environ', {'CROWDSIM_AMAP_WEB_KEY': ''}):
            response = await self.client.get(self.url + '/traffic?radius=500')
        self.assertEqual(response.status, 200)
        data = (await response.json())['data']
        self.assertEqual(data['status'], 'unconfigured')
        self.assertEqual(data['scope']['radius_m'], 500)
        self.assertIsNone(data['vehicle_count'])
        self.assertEqual(self.repo.get(self.task['id']), before)
        self.assertEqual((await self.client.get(self.url + '/traffic?radius=0')).status, 400)
        self.assertEqual((await self.client.get('/api/crowdSim/analysis-tasks/missing/domain-portrait/traffic')).status, 404)


if __name__ == '__main__':
    unittest.main()
