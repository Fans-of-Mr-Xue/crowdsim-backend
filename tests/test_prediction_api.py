"""HTTP -> saved task -> spawned native worker -> compressed result -> HTTP."""
import asyncio
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
from data_service.repositories.analysis_tasks import AnalysisTaskRepository
from data_service.repositories.pre_assessments import PreAssessmentRepository
from data_service.prediction.consultation_catalog import experiment_defaults


class PredictionLeaseTests(unittest.TestCase):
    def setUp(self):
        enable_gridfs_integration()
        self.db = mongomock.MongoClient().db
        self.runs = PreAssessmentRepository(self.db)
        self.runs.results._timeout = None
        self.db.pre_assessment_runs.insert_one({'_id': 'live', 'status': 'running',
            'lease_expires_at': '2000-01-01T00:00:00+00:00'})

    def test_live_progress_recovers_expired_lease(self):
        self.assertEqual(self.runs.get('live')['error_code'], 'lease_expired')
        self.runs.update('live', 'running', progress={'minute': 180})
        record = self.runs.get('live')
        self.assertEqual(record['status'], 'running')
        self.assertNotIn('error', record)
        self.assertNotIn('error_code', record)

    def test_completed_worker_output_survives_lease_timeout(self):
        self.runs.get('live')
        result = {'results': [{'method': {'id': 'conservation'}, 'summary': {'remaining_crowd': 12}}]}
        self.runs.complete('live', result)
        self.assertEqual(self.runs.get('live')['status'], 'completed')
        self.assertEqual(self.runs.read_result('live')['results'], result['results'])

    def test_real_failure_is_not_revived_by_late_progress(self):
        self.runs.update('live', 'failed', error='计算失败')
        self.runs.update('live', 'running', progress={'minute': 180})
        self.runs.complete('live', {'results': []})
        self.assertEqual(self.runs.get('live')['status'], 'failed')
        self.assertEqual(self.runs.get('live')['error'], '计算失败')


class PredictionLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        enable_gridfs_integration()
        self.db = mongomock.MongoClient().db
        task, _, settings = demo_inputs()
        repository = AnalysisTaskRepository(self.db)
        payload = json.loads(json.dumps({'title': task['title'], 'time_window': task['time_window'],
                   'region': {**task['region'], 'mode': 'custom'}, 'components': {'forecast_setup': settings}}))
        saved = repository.save(payload)
        # Model initialization or saving an annotation advances the task revision.
        self.task = repository.save({**payload, 'title': task['title'] + ' · 已保存'}, saved['id'], saved['revision'])
        self.add_run('saved-v1', 1, 'completed', '2026-10-22T10:00:00+00:00')
        app = web.Application()
        register_pre_assessment_routes(app, self.db, task_roads=SimpleNamespace(snapshots=SimpleNamespace(initialize=Mock())))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    def add_run(self, identifier, revision, status, created_at, **extra):
        self.db.pre_assessment_runs.insert_one({'_id': identifier, 'task_id': self.task['id'],
            'task_revision': revision, 'status': status, 'created_at': created_at,
            'lease_expires_at': '2099-01-01T00:00:00+00:00', **extra})

    async def latest(self, **params):
        response = await self.client.get('/api/crowdSim/pre-assessments/latest', params={'task_id': self.task['id'], **params})
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())['data']

    async def test_default_lookup_finds_saved_result_after_task_revision_changes(self):
        run = await self.latest()
        self.assertEqual(self.task['revision'], 2)
        self.assertEqual(run['id'], 'saved-v1')
        self.assertEqual(run['task_revision'], 1)
        self.assertIsNone(await self.latest(scope='current'))
        self.assertEqual(self.db.pre_assessment_runs.count_documents({}), 1)

    async def test_results_keep_completed_snapshot_while_control_follows_active_run(self):
        self.add_run('running-v2', 2, 'running', '2026-10-22T11:00:00+00:00')
        self.assertEqual((await self.latest())['id'], 'running-v2')
        self.assertEqual((await self.latest(prefer='completed'))['id'], 'saved-v1')
        self.db.pre_assessment_runs.update_one({'_id': 'running-v2'}, {'$set': {'status': 'completed'}})
        self.assertEqual((await self.latest(prefer='completed'))['id'], 'running-v2')

    async def test_failed_retry_and_other_task_do_not_hide_or_replace_saved_result(self):
        self.add_run('failed-v2', 2, 'failed', '2026-10-22T11:00:00+00:00')
        self.add_run('different-task', 4, 'completed', '2026-10-22T12:00:00+00:00', task_id='unrelated')
        self.assertEqual((await self.latest())['id'], 'saved-v1')
        self.assertEqual((await self.latest(prefer='completed'))['id'], 'saved-v1')
        self.assertEqual((await self.latest(scope='current'))['id'], 'failed-v2')

    async def test_recent_lookup_keeps_new_failure_visible_after_reload(self):
        self.add_run('failed-v2', 2, 'failed', '2026-10-22T11:00:00+00:00',
                     method_errors=[{'method': {'id': 'social_force'}, 'message': '越界'}])
        self.add_run('unrelated', 1, 'completed', '2026-10-22T12:00:00+00:00', task_id='other')
        latest = await self.latest(prefer='recent')
        self.assertEqual(latest['id'], 'failed-v2')
        self.assertEqual(latest['method_errors'][0]['message'], '越界')
        self.add_run('retry', 2, 'running', '2026-10-22T13:00:00+00:00',
                     lease_expires_at='2000-01-01T00:00:00+00:00')
        latest = await self.latest(prefer='recent')
        self.assertEqual(latest['id'], 'retry')
        self.assertEqual(latest['status'], 'failed')

    async def test_no_completed_result_resumes_active_run_and_expired_leases_are_not_polled(self):
        self.db.pre_assessment_runs.delete_one({'_id': 'saved-v1'})
        self.assertIsNone(await self.latest(prefer='completed'))
        self.add_run('active', 2, 'queued', '2026-10-22T11:00:00+00:00')
        self.assertEqual((await self.latest(prefer='completed'))['id'], 'active')
        self.db.pre_assessment_runs.update_one({'_id': 'active'}, {'$set': {'lease_expires_at': '2000-01-01T00:00:00+00:00'}})
        self.assertEqual((await self.latest(prefer='completed'))['status'], 'failed')


class PredictionApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_result_and_new_settings_survive_save_reload(self):
        enable_gridfs_integration()
        db = mongomock.MongoClient().db
        task, roads, settings = demo_inputs()
        chosen_ids = ['conservation', 'collision_free_speed', 'social_force']
        research = dict(objective='三模型比较', indicators=[], data_needs=[], steps=[], assumptions=[], open_questions=[],
                        experiment={'model_ids': chosen_ids, 'settings': {'cfs_time_gap_s': 1.4, 'seed': 51, 'repetitions': 2}, 'notes': '测试假设'})
        payload = json.loads(json.dumps({'title': task['title'], 'time_window': task['time_window'],
            'region': {**task['region'], 'mode': 'custom'}, 'components': {'forecast_setup': settings, 'research_plan': research}}))
        saved = AnalysisTaskRepository(db).save(payload)
        service = SimpleNamespace(snapshots=SimpleNamespace(initialize=Mock()),
                                  resolve=AsyncMock(return_value=(roads, {'id': 'test-road-snapshot'})))
        app = web.Application()
        runs = register_pre_assessment_routes(app, db, task_roads=service)
        # mongomock 4.3 synthesizes a Collection for MongoClient.options.timeout;
        # PyMongo's GridFS CSOT wrapper expects a number or None instead.
        runs.results._timeout = None
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/api/crowdSim/pre-assessments/catalog')
            contract = (await response.json())['data']
            self.assertEqual(set(contract['experiment_schema']['properties']['settings']['properties']), set(experiment_defaults()))
            self.assertIn('需要输入', contract['explanation'])
            self.assertIn('完整研究时间窗', contract['runtime_note'])
            self.assertNotIn('时长最多 60 分钟', contract['runtime_note'])
            self.assertGreater(contract['runtime_budget']['max_runtime_seconds'], 120)
            response = await client.get('/api/crowdSim/pre-assessments/methods')
            catalog = (await response.json())['data']
            chosen = [m['id'] for m in catalog if m.get('recommended')]
            self.assertEqual(len(chosen), 3)
            response = await client.post('/api/crowdSim/pre-assessments', json={
                'task_id': saved['id'], 'task_revision': saved['revision']})
            self.assertEqual(response.status, 202, await response.text())
            run_id = (await response.json())['data']['id']
            for _ in range(400):
                response = await client.get('/api/crowdSim/pre-assessments/' + run_id)
                run = (await response.json())['data']
                if run['status'] in ('completed', 'failed'):
                    break
                await asyncio.sleep(.05)
            self.assertEqual(run['status'], 'completed', run)
            response = await client.get('/api/crowdSim/pre-assessments/' + run_id + '/result')
            result = (await response.json())['data']
            self.assertEqual(result['selected_methods'], chosen)
            self.assertEqual(result['method_errors'], [])
            self.assertEqual(len(result['results']), 3)
            self.assertEqual(result['settings']['agent_radius_m'], settings['agent_radius_m'])
            self.assertEqual(result['settings']['cfs_time_gap_s'], 1.4)
            self.assertEqual(result['settings']['seed'], 51)
            self.assertEqual(result['results'][1]['diagnostics']['cfs_time_gap_s'], 1.4)
            self.assertEqual(result['road_snapshot']['snapshot']['id'], 'test-road-snapshot')
            self.assertEqual(result['results'][1]['diagnostics']['engine'], 'JuPedSim')
            self.assertEqual(result['settings']['repetitions'], 2)
            for row in result['results']:
                self.assertEqual(row['repeat_summary']['seeds'], [51, 52])
                self.assertEqual(len(row['replications']), 2)
                self.assertEqual(row['series'], row['replications'][0]['series'])
                density = [run['summary']['peak_density_p_m2'] for run in row['replications']]
                self.assertAlmostEqual(row['repeat_summary']['metrics']['peak_density_p_m2']['mean'], sum(density) / 2)
                self.assertTrue(all(run['summary']['mass_balance_error'] < .00001 for run in row['replications']))
            cfs, sfm = result['results'][1:]
            self.assertNotEqual(cfs['replications'][0]['series'][0]['agents'], cfs['replications'][1]['series'][0]['agents'])
            for index in range(2):
                self.assertEqual(cfs['replications'][index]['series'][0]['agents'], sfm['replications'][index]['series'][0]['agents'])


if __name__ == '__main__':
    unittest.main()
