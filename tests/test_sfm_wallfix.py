"""Regression of native friction physics and cross-process failure reporting."""
import asyncio
import json
import math
import pickle
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from data_service.api.pre_assessments import register_pre_assessment_routes
from data_service.prediction.errors import AllMethodsFailed
from data_service.prediction.sfm_engine import verified_sfm_engine
from data_service.prediction.demo import demo_inputs
from data_service.prediction.runtime_budget import MAX_PEOPLE
from data_service.repositories.analysis_tasks import AnalysisTaskRepository


class WallFrictionTests(unittest.TestCase):
    def test_native_wall_contact_dissipates_tangential_energy(self):
        verified_sfm_engine.cache_clear()
        result = verified_sfm_engine()
        self.assertAlmostEqual(result['wall_friction_velocity_after_mps'], 1 / 1.15)
        self.assertLess(result['wall_friction_velocity_after_mps'] ** 2, 1)

    def test_stiff_corner_contact_does_not_amplify_velocity(self):
        import jupedsim as jps
        sim = jps.Simulation(model=jps.SocialForceModel(body_force=0),
                             geometry=[(0, 0), (5, 0), (5, 5), (0, 5)], dt=.01)
        stage = sim.add_direct_steering_stage()
        journey = sim.add_journey(jps.JourneyDescription([stage]))
        identifier = sim.add_agent(jps.SocialForceModelAgentParameters(
            position=(.101, .101), velocity=(1, 1), radius=.2,
            desired_speed=math.sqrt(2), obstacle_scale=0,
            journey_id=journey, stage_id=stage))
        sim.agent(identifier).target = (4, 4)
        sim.iterate()
        # Two perpendicular contacts, no normal force or driving acceleration.
        # Explicit Euler would reverse and amplify each component to -1.97.
        expected = 1 / (1 + .01 * 240000 * (.2 - .101) / 80)
        for component in sim.agent(identifier).model.velocity:
            self.assertAlmostEqual(component, expected)
            self.assertGreater(component, 0)
            self.assertLess(component, 1)

    def test_pair_contact_preserves_momentum_and_dissipates_energy(self):
        import jupedsim as jps
        sim = jps.Simulation(model=jps.SocialForceModel(body_force=0),
                             geometry=[(-5, -5), (5, -5), (5, 5), (-5, 5)], dt=.01)
        stage = sim.add_direct_steering_stage()
        journey = sim.add_journey(jps.JourneyDescription([stage]))
        ids = []
        for x, vy in [(0, 1), (.21, -1)]:
            identifier = sim.add_agent(jps.SocialForceModelAgentParameters(
                position=(x, 0), velocity=(0, vy), radius=.2,
                desired_speed=1, agent_scale=0, obstacle_scale=0,
                journey_id=journey, stage_id=stage))
            sim.agent(identifier).target = (x, 4 * vy)
            ids.append(identifier)
        sim.iterate()
        velocities = [sim.agent(identifier).model.velocity for identifier in ids]
        expected = (1 - 5.7) / (1 + 5.7)
        self.assertAlmostEqual(velocities[0][1], expected)
        self.assertAlmostEqual(velocities[1][1], -expected)
        self.assertAlmostEqual(sum(v[1] for v in velocities), 0)
        self.assertLess(sum(x*x + y*y for x, y in velocities), 2)

    def test_all_failed_exception_preserves_method_details_across_processes(self):
        errors = [{'method': {'id': 'sfm', 'name': '社会力'}, 'message': '越界'}]
        restored = pickle.loads(pickle.dumps(AllMethodsFailed(errors)))
        self.assertEqual(restored.method_errors, errors)
        self.assertEqual(str(restored), '社会力：越界')


class FailedMethodsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_failed_worker_persists_each_selected_model_and_reason(self):
        enable_gridfs_integration()
        db = mongomock.MongoClient().db
        task, roads, settings = demo_inputs()
        settings.update(initial_crowd=MAX_PEOPLE + 1, repetitions=1)
        payload = json.loads(json.dumps({'title': task['title'], 'time_window': task['time_window'],
            'region': {**task['region'], 'mode': 'custom'}, 'components': {'forecast_setup': settings}}))
        saved = AnalysisTaskRepository(db).save(payload)
        service = SimpleNamespace(snapshots=SimpleNamespace(initialize=Mock()),
            resolve=AsyncMock(return_value=(roads, {'id': 'failure-test-road'})))
        app = web.Application()
        runs = register_pre_assessment_routes(app, db, task_roads=service)
        runs.results._timeout = None
        async with TestClient(TestServer(app)) as client:
            selected = ['collision_free_speed', 'social_force']
            response = await client.post('/api/crowdSim/pre-assessments', json={'task_id': saved['id'],
                'task_revision': saved['revision'], 'methods': selected})
            self.assertEqual(response.status, 202, await response.text())
            run = (await response.json())['data']
            for _ in range(200):
                response = await client.get('/api/crowdSim/pre-assessments/' + run['id'])
                run = (await response.json())['data']
                if run['status'] == 'failed':
                    break
                await asyncio.sleep(.05)
            self.assertEqual(run['status'], 'failed')
            self.assertEqual(run['methods'], selected)
            self.assertEqual([row['method']['id'] for row in run['method_errors']], selected)
            self.assertTrue(all(str(MAX_PEOPLE) in row['message'] for row in run['method_errors']))
