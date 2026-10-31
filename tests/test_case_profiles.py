"""Case migration, optimistic editing and versioned HTML attachment contracts."""
import asyncio
import copy
import io
import json
import unittest
from unittest.mock import patch

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from data_service.api.cases import register_case_routes
from data_service.repositories.cases import CaseRepository, RESOURCES
from data_service.repositories.datasets import DatasetConflictError
from data_service.schemas.cases import FIELDS, validate_case
from data_service.services.case_detail_html import render_case_html

enable_gridfs_integration()


def payload(item):
    return {key: copy.deepcopy(item[key]) for key in FIELDS if key in item}


def mock_repository(database):
    repository = CaseRepository(database)
    # mongomock has no real PyMongo timeout option; keep production GridFS intact.
    repository.pages._timeout = None
    return repository


class CaseRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().crowdsim
        self.repository = mock_repository(self.db)
        self.repository.initialize()

    def test_all_twelve_cases_and_full_project_data_survive_migration(self):
        seeds = json.loads((RESOURCES / 'case_profiles.json').read_text())
        self.assertEqual(self.repository.list()['stats'], {'total': 12, 'reviewed': 11, 'crowd': 5, 'flood': 7})
        for seed in seeds:
            stored = self.repository.get(seed['id'])
            self.assertEqual(payload(stored), payload(validate_case(payload(seed))))
            self.assertEqual(stored['eventRecord']['id'], seed['id'])
        lyon = self.repository.get('madras-lyon-2022')
        self.assertEqual(len(lyon['projectProfile']['cameras']), 11)
        self.assertEqual(len(lyon['projectProfile']['densitySeries']), 31)
        self.assertEqual(len(lyon['detailPages']), 1)
        stream, _ = self.repository.open_page(lyon['id'], lyon['detailPage']['id'])
        raw = stream.read().decode()
        self.assertEqual(raw, render_case_html(lyon))
        self.assertIn('data:image/png;base64,', raw)
        for camera in lyon['projectProfile']['cameras']:
            self.assertIn('id="' + camera['id'] + '"', raw)
            for url in camera['videos']:
                self.assertIn(url.replace('&', '&amp;'), raw)

    def test_restart_preserves_edits_deleted_cases_and_attachment_versions(self):
        first = self.repository.get('zhengzhou-2021')
        data = payload(first)
        data.update(name='数据库已编辑案例', locationType='测试空间', tags=['新增标签'], sourceUrl='https://example.org/case')
        self.repository.save(data, first['id'], first['revision'])
        removed = self.repository.get('seoul-crowd-2022')
        self.repository.delete(removed['id'], removed['revision'])
        lyon = self.repository.get('madras-lyon-2022')
        saved = self.repository.attach_page(lyon['id'], lyon['revision'], io.BytesIO(b'<html>updated</html>'), 'new.html')
        next_repository = mock_repository(self.db)
        next_repository.initialize()
        self.assertEqual(payload(next_repository.get(first['id'])), data)
        self.assertIsNone(next_repository.get(removed['id']))
        self.assertEqual(next_repository.get(lyon['id'])['detailPage']['id'], saved['detailPage']['id'])
        self.assertEqual(self.db.case_pages.files.count_documents({}), 2)

    def test_metadata_edits_retain_reserved_links_and_reject_stale_writes(self):
        case = self.repository.save({'name': '关联接口测试', 'extensions': {'customFact': ['可扩展']}})
        linked = self.repository.set_links(case['id'], case['revision'], {'linkedPlanIds': ['plan-1', 'plan-1'], 'linkedModelIds': ['model-1']})
        data = payload(linked)
        del data['linkedPlanIds'], data['linkedModelIds']
        data['summary'] = '编辑摘要'
        saved = self.repository.save(data, case['id'], linked['revision'])
        self.assertEqual(saved['linkedPlanIds'], ['plan-1'])
        self.assertEqual(saved['linkedModelIds'], ['model-1'])
        self.assertEqual(saved['extensions'], {'customFact': ['可扩展']})
        for operation in (lambda: self.repository.save(data, case['id'], 1), lambda: self.repository.delete(case['id'], 1), lambda: self.repository.set_links(case['id'], 1, {})):
            with self.assertRaises(DatasetConflictError):
                operation()
        with self.assertRaises(DatasetConflictError):
            self.repository.save({'name': case['name']})

    def test_html_versions_are_deduplicated_isolated_and_atomically_attached(self):
        case = self.repository.save({'name': '详情页测试'})
        one = self.repository.attach_page(case['id'], 1, io.BytesIO(b'<html>one</html>'), 'one.html')
        duplicate = self.repository.attach_page(case['id'], one['revision'], io.BytesIO(b'<html>one</html>'), 'same.htm')
        self.assertEqual(one, duplicate)
        two = self.repository.attach_page(case['id'], one['revision'], io.BytesIO(b'<html>two</html>'), 'two.html')
        self.assertEqual(len(two['detailPages']), 2)
        self.assertEqual(self.repository.open_page(case['id'], one['detailPage']['id'])[0].read(), b'<html>one</html>')
        with self.assertRaises(KeyError):
            self.repository.open_page('zhengzhou-2021', one['detailPage']['id'])
        count = self.db.case_pages.files.count_documents({})
        with patch.object(self.repository, 'change', side_effect=DatasetConflictError('concurrent update')):
            with self.assertRaises(DatasetConflictError):
                self.repository.attach_page(case['id'], two['revision'], io.BytesIO(b'<html>three</html>'), 'three.html')
        self.assertEqual(self.db.case_pages.files.count_documents({}), count)
        self.repository.delete(case['id'], two['revision'])
        with self.assertRaises(KeyError):
            self.repository.open_page(case['id'], one['detailPage']['id'])

    def test_invalid_case_metadata_and_non_html_attachments_are_rejected(self):
        for data in ({'name': ' '}, {'name': 'x', 'sourceUrl': 'javascript:alert(1)'}, {'name': 'x', 'coordinates': [91, 12]}, {'name': 'x', 'coordinates': [True, 10]}, {'name': 'x', 'extensions': {'$where': 'bad'}}, {'name': 'x', 'details': [['bad']]}, {'name': 'x', 'tags': ['']}, {'name': 'x', 'projectProfile': {'density': float('nan')}}):
            with self.assertRaises(ValueError):
                validate_case(data)
        case = self.repository.save({'name': '附件验证'})
        for filename, raw in (('x.txt', b'<html>x</html>'), ('x.html', b'plain text'), ('x.html', b''), ('x.html', b'\xff'), ('x.html', b'<html>\x00</html>')):
            with self.assertRaises(ValueError):
                self.repository.attach_page(case['id'], 1, io.BytesIO(raw), filename)
        with patch('data_service.repositories.cases.MAX_PAGE_BYTES', 8):
            with self.assertRaises(ValueError):
                self.repository.attach_page(case['id'], 1, io.BytesIO(b'<html>large</html>'), 'large.html')


class CaseApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = mongomock.MongoClient().crowdsim
        app = web.Application()
        repository = register_case_routes(app, self.db)
        repository.pages._timeout = None
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        for _ in range(100):
            response = await self.client.get('/api/crowdSim/cases')
            if response.status == 200:
                break
            await asyncio.sleep(.01)
        self.assertEqual(response.status, 200)

    async def asyncTearDown(self):
        await self.client.close()

    async def test_crud_links_and_html_upload_download(self):
        root = '/api/crowdSim/cases'
        response = await self.client.post(root, json={'name': '接口测试', 'tags': ['洪涝'], 'locationType': '地下空间'})
        self.assertEqual(response.status, 201)
        created = (await response.json())['data']
        path = root + '/' + created['id']
        response = await self.client.put(path + '/links', json={'revision': 1, 'linkedPlanIds': ['p'], 'linkedModelIds': ['m']})
        self.assertEqual(response.status, 200)
        form = FormData()
        # Intentionally put file before revision to support either multipart order.
        form.add_field('file', b'<html><body><script>window.x=1</script>upload</body></html>', filename='详情.html', content_type='text/html')
        form.add_field('revision', '2')
        response = await self.client.post(path + '/pages', data=form)
        self.assertEqual(response.status, 201, await response.text())
        attached = (await response.json())['data']
        self.assertEqual(attached['linkedPlanIds'], ['p'])
        page_url = attached['detailPage']['url']
        response = await self.client.get(page_url)
        self.assertEqual(response.status, 200)
        self.assertIn('upload', await response.text())
        self.assertIn('sandbox allow-scripts', response.headers['Content-Security-Policy'])
        self.assertNotIn('allow-same-origin', response.headers['Content-Security-Policy'])
        self.assertIn("connect-src 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        response = await self.client.get(page_url + '?download=1')
        self.assertTrue(response.headers['Content-Disposition'].startswith('attachment'))
        self.assertEqual((await self.client.put(path, json={'revision': 1, 'name': '旧编辑'})).status, 409)
        self.assertEqual((await self.client.get('/crowdSim/cases/' + created['id'])).status, 200)
        self.assertEqual((await self.client.delete(path, json={'revision': attached['revision']})).status, 200)
        self.assertEqual((await self.client.get(page_url)).status, 404)

    async def test_invalid_requests_and_missing_cases(self):
        root = '/api/crowdSim/cases'
        for data in ([], {'name': 'x', '_id': 'injection'}):
            self.assertEqual((await self.client.post(root, json=data)).status, 400)
        self.assertEqual((await self.client.put(root + '/missing', json={'name': 'x'})).status, 400)
        self.assertEqual((await self.client.delete(root + '/missing', json={'revision': 1, 'other': True})).status, 400)
        self.assertEqual((await self.client.post(root + '/missing/pages', json={'revision': 1})).status, 400)
        self.assertEqual((await self.client.get(root + '/missing')).status, 404)
        self.assertEqual((await self.client.put(root + '/missing/links', json={'revision': 1, 'name': 'x'})).status, 400)
