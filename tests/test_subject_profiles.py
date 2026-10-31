"""Subject data persistence, revision conflicts and HTTP contracts."""
import asyncio
import copy
import json
import unittest

import mongomock
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from data_service.api.subjects import register_subject_routes
from data_service.repositories.datasets import DatasetConflictError
from data_service.repositories.subjects import SEEDS, SubjectRepository
from data_service.schemas.subjects import FIELDS, validate_subject


def payload(document):
    return {key: copy.deepcopy(document[key]) for key in FIELDS if key in document}


class SubjectRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().crowdsim
        self.repository = SubjectRepository(self.db)
        self.repository.initialize()

    def test_all_original_profiles_round_trip_without_loss(self):
        seeds = json.loads(SEEDS.read_text())
        self.assertEqual(self.repository.list()['stats'], {'total': 11, 'crowd': 6, 'support': 5, 'behaviorDimensions': 36})
        for seed in seeds:
            self.assertEqual(payload(self.repository.get(seed['id'])), payload(seed))

    def test_edits_persist_after_reinitialization_and_reject_stale_revision(self):
        existing = self.repository.list()['items'][0]
        data = payload(existing)
        data['name'] = '修改后的主体'
        data['agentPortrait']['metrics'][0][1] = 27
        data['agentPortrait']['personas'][0]['badges'].append('新标签')
        saved = self.repository.save(data, existing['id'], existing['revision'])
        self.assertEqual(saved['revision'], 2)
        with self.assertRaises(DatasetConflictError):
            self.repository.save(payload(existing), existing['id'], existing['revision'])
        new_repository = SubjectRepository(self.db)
        new_repository.initialize()
        self.assertEqual(payload(new_repository.get(existing['id'])), data)

    def test_soft_deletion_is_not_restored_by_seed_and_name_can_be_reused(self):
        existing = self.repository.list()['items'][0]
        self.repository.delete(existing['id'], existing['revision'])
        self.repository.initialize()
        self.assertIsNone(self.repository.get(existing['id']))
        self.assertTrue(self.db.subject_profiles.find_one({'_id': existing['id']})['is_deleted'])
        created = self.repository.save(payload(existing))
        self.assertNotEqual(created['id'], existing['id'])
        self.assertEqual(created['origin'], 'custom')

    def test_duplicate_name_and_stale_deletion_are_rejected(self):
        existing = self.repository.list()['items'][0]
        with self.assertRaises(DatasetConflictError):
            self.repository.save(payload(existing))
        self.repository.save(payload(existing), existing['id'], 1)
        with self.assertRaises(DatasetConflictError):
            self.repository.delete(existing['id'], 1)

    def test_invalid_portraits_cannot_break_editors(self):
        original = payload(self.repository.list()['items'][0])
        for mutate in (
            lambda d: d.update(name=' '),
            lambda d: d['agentPortrait'].update(metrics=[['指标', 101, '过大']]),
            lambda d: d['agentPortrait'].update(personas=[{'name': [], 'badges': []}]),
            lambda d: d['agentClass'].update(classId=''),
            lambda d: d['agentClass'].update(defaultCount=True),
            lambda d: d['agentClass'].update(llmConfig={'$where': 'invalid'}),
            lambda d: d['agentPortrait'].update(metrics=[['指标', float('nan'), '无效']]),
        ):
            data = copy.deepcopy(original)
            mutate(data)
            with self.assertRaises(ValueError):
                validate_subject(data)


class SubjectApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = mongomock.MongoClient().crowdsim
        app = web.Application()
        register_subject_routes(app, self.db)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        for _ in range(100):
            response = await self.client.get('/api/crowdSim/subject-profiles')
            if response.status == 200:
                break
            await asyncio.sleep(.01)
        self.assertEqual(response.status, 200)

    async def asyncTearDown(self):
        await self.client.close()

    async def test_create_update_read_conflict_delete(self):
        root = '/api/crowdSim/subject-profiles'
        data = {'name': '数据库测试主体', 'topic': '人群主体', 'agentClass': {'classId': 'Test'},
                'agentPortrait': {'role': '测试角色', 'tags': ['新标签'], 'personas': []}}
        response = await self.client.post(root, json=data)
        self.assertEqual(response.status, 201)
        created = (await response.json())['data']
        path = root + '/' + created['id']
        data['agentPortrait']['tags'].append('修改标签')
        response = await self.client.put(path, json={**data, 'revision': 1})
        self.assertEqual(response.status, 200)
        response = await self.client.put(path, json={**data, 'revision': 1})
        self.assertEqual(response.status, 409)
        response = await self.client.get(path)
        self.assertEqual((await response.json())['data']['agentPortrait']['tags'], ['新标签', '修改标签'])
        self.assertEqual((await self.client.delete(path, json={'revision': 2})).status, 200)
        self.assertEqual((await self.client.get(path)).status, 404)
        self.assertEqual((await self.client.get('/crowdSim/subject-profiles')).status, 200)

    async def test_invalid_requests(self):
        root = '/api/crowdSim/subject-profiles'
        self.assertEqual((await self.client.post(root, json=[])).status, 400)
        self.assertEqual((await self.client.post(root, json={'name': '测试', '_id': 'injected'})).status, 400)
        self.assertEqual((await self.client.put(root + '/missing', json={'name': '测试'})).status, 400)
        self.assertEqual((await self.client.delete(root + '/missing', json={'revision': 1, 'extra': True})).status, 400)
