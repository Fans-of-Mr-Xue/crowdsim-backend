import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import mongomock
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from mongomock.gridfs import enable_gridfs_integration

from data_service.api.domain_portrait import register_domain_portrait_routes
from data_service.api.pre_assessments import register_pre_assessment_routes
from data_service.prediction.demo import demo_inputs
from data_service.repositories.analysis_tasks import AnalysisTaskRepository
from data_service.services.domain_demand import gate_scope
from data_service.services.domain_portrait import build_portrait
from data_service.services.forecast_demand import resolve_forecast_inputs


class GateProfileTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task, self.roads, settings = demo_inputs()
        task['time_window'] = '2026-12-31 23:55 至 2027-01-01 00:05'
        task['components']['forecast_setup'] = settings
        task['region']['mode'] = 'custom'
        self.task = task
        gates = sorted(settings.get('exit_points') or 'ABC')
        self.rows = [dict(minute=m, gate=g, **{'in':10 if i == 0 else 0, 'out':3})
                     for m in [0,5] for i,g in enumerate(gates)]
        self.rows[-1]['in'] = 5
        self.dataset = dict(source='回归样例', kind='scenario', observed_start='2026-12-31T23:55',
                            scope=gate_scope(task), records=self.rows)
        self.task['components']['domain_portrait'] = {'gate_counts':self.dataset}

    def test_observed_reference_and_stale_strength(self):
        self.task['time_window'] = '2020-12-31 23:55 至 2021-01-01 00:05'
        self.dataset.update(scope=gate_scope(self.task), observed_start='2020-12-31T23:55', kind='observed')
        def strength():
            return build_portrait(self.task)['evidence_strengths']['gateCounts']['score']
        self.assertEqual(strength(), 3)
        self.dataset.update(observed_start='2019-12-31T23:55', kind='reference')
        self.assertEqual(strength(), 2)
        self.dataset['records'].pop()
        self.assertIsNone(strength())

    def test_integral_shares_stale_and_manual(self):
        settings, source = resolve_forecast_inputs(self.task, {'arrival_rate_per_minute':777}, strict=True)
        self.assertEqual([r['rate'] for r in settings['arrival_schedule']], [2,3])
        self.assertEqual(source['total_visits'], 25)
        self.assertEqual(settings['entrance_shares'][0], .8)
        self.assertEqual(source['gate_counts']['left'], 3*len(self.rows))
        self.assertEqual(source['activity_decisions'], [])
        self.task['time_window'] = '2026-12-31 23:56 至 2027-01-01 00:06'
        with self.assertRaises(ValueError):
            resolve_forecast_inputs(self.task, strict=True)
        self.task['components']['forecast_demand_source'] = {'mode':'manual'}
        self.assertEqual(resolve_forecast_inputs(self.task, {'arrival_rate_per_minute':7},strict=True)[0]['arrival_rate_per_minute'],7)

    def test_missing_duplicate_negative_and_zero(self):
        for rows in [self.rows[:-1], self.rows[:-1]+[self.rows[0]], [{**self.rows[0],'in':-1},*self.rows[1:]]]:
            self.dataset['records'] = rows
            with self.assertRaises(ValueError):
                resolve_forecast_inputs(self.task,strict=True)
        self.dataset['records'] = [{**r,'in':0} for r in self.rows]
        _, origin = resolve_forecast_inputs(self.task,strict=True)
        self.assertEqual(origin['total_visits'],0)
        self.assertNotIn('entrance_shares',origin['forecast_inputs'])

    async def test_http_import_read_context_and_withdraw(self):
        enable_gridfs_integration()
        db = mongomock.MongoClient().db
        payload = json.loads(json.dumps({k:self.task[k] for k in ('title','time_window','region','components')}))
        payload['components'].pop('domain_portrait')
        saved = AnalysisTaskRepository(db).save(payload)
        app = web.Application()
        roads = SimpleNamespace(snapshots=SimpleNamespace(initialize=Mock()), describe=AsyncMock(return_value={}),
                                resolve=AsyncMock(return_value=(self.roads, {'id':'gate-test-road'})),
                                task_context=AsyncMock(side_effect=lambda t:{**t,'road_network':{}}))
        register_domain_portrait_routes(app,db,task_roads=roads)
        runs = register_pre_assessment_routes(app,db,task_roads=roads)
        runs.results._timeout = None
        path='/api/crowdSim/analysis-tasks/'+saved['id']+'/domain-portrait'
        async with TestClient(TestServer(app)) as client:
            before=(await (await client.get(path)).json())['data']
            self.assertIsNone(before['portrait']['evidence_strengths']['gateCounts']['score'])
            dataset={**self.dataset,'scope':before['portrait']['gate_count_scope']}
            async def put(data,rev):
                return await client.put(path,json={'section':'gate_counts','revision':rev,'data':data})
            res=await put(dataset,saved['revision'])
            self.assertEqual(res.status,200,await res.text())
            after=(await res.json())['data']
            self.assertEqual(after['portrait']['evidence_strengths']['gateCounts']['score'], 0)
            self.assertNotIn('evidence_strengths', after['task']['components']['domain_portrait'])
            ctx=(await (await client.get('/api/crowdSim/pre-assessments/context',params={'task_id':saved['id']})).json())['data']
            self.assertEqual(ctx['input_provenance']['gate_counts'],after['portrait']['demand']['gate_counts'])
            self.assertEqual(ctx['settings']['arrival_schedule'],after['portrait']['demand']['forecast_inputs']['arrival_schedule'])
            base = '/api/crowdSim/pre-assessments'
            run = await client.post(base, json={'task_id':saved['id'],'task_revision':after['task']['revision'],
                                                'methods':['conservation'],'settings':{'arrival_rate_per_minute':999}})
            self.assertEqual(run.status,202,await run.text())
            identifier = (await run.json())['data']['id']
            for _ in range(200):
                state = (await (await client.get(base+'/'+identifier)).json())['data']
                if state['status'] in ('completed','failed'):
                    break
                await asyncio.sleep(.05)
            self.assertEqual(state['status'],'completed',state)
            output = (await (await client.get(base+'/'+identifier+'/result')).json())['data']
            self.assertEqual(output['results'][0]['series'][-1]['cumulative_arrivals'],25)
            self.assertEqual(output['task']['components']['forecast_input_provenance']['gate_counts'],ctx['input_provenance']['gate_counts'])
            self.assertEqual((await put(None,saved['revision'])).status,409)
            self.assertEqual((await put(None,after['task']['revision'])).status,200)
