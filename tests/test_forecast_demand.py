"""Domain estimate -> console context -> real worker -> immutable results."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from data_service.api.pre_assessments import register_pre_assessment_routes
from data_service.prediction.demo import demo_inputs
from data_service.prediction.inputs import default_settings
from data_service.repositories.analysis_tasks import AnalysisTaskRepository
from data_service.services import domain_demand
from data_service.services.forecast_demand import resolve_forecast_inputs


def domain_task():
    task, roads, settings = demo_inputs()
    settings.update(repetitions=1)
    task['components']['forecast_setup'] = settings
    history = {'title': '测试客流', 'scope': '虚构广场', 'evidence_id': 'test',
               'records': [{'date': '2025-10-21', 'daily_visits': 2000}]}
    config = domain_demand.initialize(task, {'visualizations': {'history': history}})
    config['assumptions'].update(local_share=.1, window_share=.5)
    # 2000 * .1 * .5 * .9 = 90 new arrivals; 60 initially present is separate.
    task['components']['domain_portrait'] = {'demand_inputs': config}
    return task, roads


class ForecastDemandTests(unittest.TestCase):
    def test_domain_overrides_old_curve_without_changing_initial_or_saved_task(self):
        task, _ = domain_task()
        before = copy.deepcopy(task)
        settings, source = resolve_forecast_inputs(task, {'arrival_schedule': [], 'arrival_rate_per_minute': 1}, strict=True)
        self.assertEqual(source['mode'], 'domain')
        self.assertEqual(source['total_visits'], 90)
        self.assertEqual(settings['initial_crowd'], 60)
        self.assertEqual(settings['dwell_minutes'], 1)
        self.assertEqual(settings['arrival_schedule'][0]['rate'], 30)
        self.assertEqual(task, before)
        task['components']['domain_portrait']['demand_inputs']['assumptions']['window_share'] = 1
        changed, origin = resolve_forecast_inputs(task, strict=True)
        self.assertEqual(origin['total_visits'], 180)
        self.assertNotEqual(changed['arrival_schedule'], settings['arrival_schedule'])
        self.assertNotEqual(origin['fingerprint'], source['fingerprint'])

    def test_stale_scope_blocks_domain_but_explicit_manual_preserves_curve(self):
        task, _ = domain_task()
        task['time_window'] = '2026-10-21 18:00 至 2026-10-21 18:04'
        self.assertEqual(resolve_forecast_inputs(task)[1]['status'], 'stale')
        with self.assertRaisesRegex(ValueError, '领域客流'):
            resolve_forecast_inputs(task, strict=True)
        task['components']['forecast_demand_source'] = {'mode': 'manual'}
        settings, source = resolve_forecast_inputs(task, strict=True)
        self.assertEqual(settings, default_settings(task))
        self.assertEqual(source['mode'], 'manual')

    def test_legacy_tasks_keep_inputs_and_unknown_fields_are_rejected(self):
        task, _, settings = demo_inputs()
        task['components']['forecast_setup'] = settings
        self.assertEqual(resolve_forecast_inputs(task, strict=True)[0], default_settings(task))
        self.assertEqual(resolve_forecast_inputs(task)[1]['mode'], 'manual')
        for overrides in ({'invented': 1}, ['bad']):
            with self.assertRaises(ValueError):
                resolve_forecast_inputs(task, overrides, strict=True)
        task['components']['forecast_demand_source'] = {'mode': 'domain'}
        with self.assertRaises(ValueError):
            resolve_forecast_inputs(task, strict=True)
        task['components'].pop('forecast_demand_source')
        task['components']['domain_portrait'] = {'demand_inputs': {}}
        self.assertEqual(resolve_forecast_inputs(task)[1]['status'], 'stale')
        with self.assertRaises(ValueError):
            resolve_forecast_inputs(task, strict=True)


class ForecastDemandApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_estimate_reaches_all_three_real_solvers_and_frozen_result(self):
        enable_gridfs_integration()
        db = mongomock.MongoClient().db
        task, roads = domain_task()
        payload = json.loads(json.dumps({key: task[key] for key in ('title', 'time_window', 'region', 'components')}))
        payload['region']['mode'] = 'custom'
        repo = AnalysisTaskRepository(db)
        saved = repo.save(payload)
        service = SimpleNamespace(snapshots=SimpleNamespace(initialize=Mock()),
            describe=AsyncMock(return_value={'status': 'available'}),
            resolve=AsyncMock(return_value=(roads, {'id': 'domain-test-road'})))
        app = web.Application()
        runs = register_pre_assessment_routes(app, db, task_roads=service)
        runs.results._timeout = None
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/api/crowdSim/pre-assessments/context', params={'task_id': saved['id']})
            self.assertEqual(response.status, 200, await response.text())
            context = (await response.json())['data']
            self.assertEqual(context['input_provenance']['total_visits'], 90)
            response = await client.post('/api/crowdSim/pre-assessments', json={
                'task_id': saved['id'], 'task_revision': saved['revision'],
                'methods': ['conservation', 'collision_free_speed', 'social_force'],
                'settings': {'arrival_schedule': [], 'arrival_rate_per_minute': 1}})
            self.assertEqual(response.status, 202, await response.text())
            record = (await response.json())['data']
            self.assertEqual(record['settings'], context['settings'])
            self.assertEqual(record['input_provenance'], context['input_provenance'])
            for _ in range(600):
                response = await client.get('/api/crowdSim/pre-assessments/' + record['id'])
                status = (await response.json())['data']
                if status['status'] in ('completed', 'failed'):
                    break
                await asyncio.sleep(.05)
            self.assertEqual(status['status'], 'completed', status)
            url = '/api/crowdSim/pre-assessments/' + record['id'] + '/result'
            response = await client.get(url)
            result = (await response.json())['data']
            self.assertEqual(result['method_errors'], [])
            self.assertEqual(len(result['results']), 3)
            for row in result['results']:
                final = row['series'][-1]
                self.assertAlmostEqual(final['cumulative_arrivals'], 90, delta=.01)
                self.assertAlmostEqual(final['crowd'] + final['outside_queue'] + final['departed'], 150, delta=.02)
                self.assertLess(row['summary']['mass_balance_error'], .00001)
            self.assertEqual(result['task']['components']['forecast_input_provenance'], context['input_provenance'])
            payload['components']['domain_portrait']['demand_inputs']['assumptions']['window_share'] = 1
            repo.save(payload, saved['id'], saved['revision'])
            fresh = await client.get('/api/crowdSim/pre-assessments/context', params={'task_id': saved['id']})
            self.assertEqual((await fresh.json())['data']['input_provenance']['total_visits'], 180)
            frozen = await client.get(url)
            self.assertEqual((await frozen.json())['data'], result)
