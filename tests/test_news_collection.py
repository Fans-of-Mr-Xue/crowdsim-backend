"""News crawler contract tests; run with pytest/mongomock from requirements.txt."""
import asyncio
from datetime import timedelta
import unittest

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from data_service.api.news import register_news_routes
from data_service.crawlers.news_sources import canonical_url, extract_article, parse_published, search_links
from data_service.crawlers.locations import article_locations
from data_service.crawlers.worker import run_worker
from data_service.repositories.news import NewsRepository, now
from data_service.schemas.news import validate_task
from data_service.connectors.llm import JsonModelClient, ModelError
from data_service.services.news_keywords import recommend_keywords


def config(**changes):
    result = dict(eventName='成都暴雨', regionId='chengdu', location='成都 · 中国', regionKeyword='成都',
                  keywords=['暴雨'], timeRange=['2021-07-20 00:00', '2021-07-30 23:59'], maxRecords=2,
                  seedUrls=[])
    result.update(changes)
    return result


def news_database():
    enable_gridfs_integration()
    database = mongomock.MongoClient().crowdsim
    database.environment_profiles.insert_many([
        {'_id': 'chengdu', 'locationLabel': '成都 · 中国', 'is_deleted': False},
        {'_id': 'zhengzhou-portrait-region', 'locationLabel': '郑州', 'is_deleted': False},
    ])
    return database


def html(title='成都暴雨救援', region='成都', topic='暴雨', date='2021-07-21T12:00:00+08:00'):
    return f'''<html><head><meta property="og:title" content="{title}">
    <meta property="article:published_time" content="{date}"><meta name="source" content="测试媒体"></head>
    <body><nav>其它城市 暴雨</nav><article><p>{region}{topic}{title}。{'抢险救援人员转移受困群众，交通部门排查城市道路积水。' * 8}</p></article></body></html>'''


def article(task, url='https://example.com/news/1', title='成都暴雨救援'):
    return extract_article(html(title), url, task)


class NewsSchemaTests(unittest.TestCase):
    def test_places_need_original_mentions_and_matching_region(self):
        text = '郑州暴雨，沙口路站和海滩寺街站停运，河医院区正在转移患者。'
        matched = article_locations('郑州暴雨', text, 'zhengzhou-portrait-region')
        self.assertEqual([point['name'] for point in matched['locationPoints']],
                         ['沙口路站', '海滩寺站', '郑大一附院河医院区'])
        self.assertEqual(len({tuple(point['coordinates']) for point in matched['locationPoints']}), 3)
        for point in matched['locationPoints']:
            self.assertIn(point['matchedText'], point['evidence'])
            self.assertIn(point['matchedText'], text)
            self.assertEqual(point['coordinateSystem'], 'WGS84')
            self.assertTrue(point['sourceUrl'].startswith('https://www.openstreetmap.org/'))
        self.assertEqual(article_locations('成都暴雨', text, 'chengdu')['locationPoints'], [])

    def test_broad_region_or_line_and_ambiguous_hospital_are_not_pois(self):
        for text in ['郑州市暴雨', '郑州地铁5号线停运', '郑州大学第一附属医院郑东院区']:
            with self.subTest(text=text):
                result = article_locations(text, text, 'zhengzhou-portrait-region')
                self.assertEqual(result['locationPoints'], [])
                self.assertEqual(result['locationPrecision'], 'task_region')

    def test_place_aliases_are_deduplicated(self):
        result = article_locations('海滩寺街站', '海滩寺站、海滩寺街站', 'zhengzhou-portrait-region')
        self.assertEqual(len(result['locationPoints']), 1)

    def test_shanghai_wall_time_is_explicit_utc(self):
        task = validate_task(config())
        self.assertEqual(task['timeRange'][0], '2021-07-19T16:00:00+00:00')
        self.assertEqual(parse_published('Wed, 21 Jul 2021 04:00:00 GMT').isoformat(), '2021-07-21T04:00:00+00:00')
        self.assertEqual(parse_published('2021年07月21日 12:30').hour, 4)
        self.assertEqual(parse_published('2021年07月21日 1:30').hour, 17)

    def test_invalid_configuration_is_rejected(self):
        for changes in ({'maxRecords': True}, {'maxRecords': 0}, {'maxRecords': 501}, {'keywords': []}, {'keywords': ['成都']},
                        {'timeRange': ['2021-07-21', '2021-07-20']}, {'seedUrls': ['http://127.0.0.1/test']},
                        {'seedUrls': ['file:///tmp/test']}, {'eventName': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_task(config(**changes))

    def test_article_requires_region_topic_date_and_body(self):
        task = validate_task(config())
        for raw in (html(region='北京', title='北京暴雨'), html(topic='晴天', title='成都天气'),
                    html(date='2022-01-01'), html(date=''), '<h1>成都暴雨</h1>'):
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                extract_article(raw, 'https://example.com/a', task)
        verified = article(task)
        self.assertTrue(verified['verified'])
        self.assertNotIn('coordinates', verified)

    def test_tracking_links_are_canonicalized(self):
        self.assertEqual(canonical_url('https://EXAMPLE.com/a?utm_source=test&b=2#x'), 'https://example.com/a?b=2')
        with self.assertRaises(ValueError):
            search_links('<html><title>百度安全验证</title></html>', 'https://www.baidu.com')

    def test_legacy_chinanews_date_and_body_exclude_sidebar_news(self):
        task = validate_task(config(eventName='上海外滩跨年夜', regionId='shanghai', regionKeyword='上海',
            keywords=['外滩'], timeRange=['2014-12-01 00:00', '2015-01-31 23:59']))
        body = '上海外滩跨年夜的客流组织与人群安全报道。' * 12
        raw = f'<h1>上海外滩跨年夜现场报道</h1><span id="pubtime_baidu">2015-01-02 05:22:31</span><div class="content"><div class="left_zw"><p>{body}</p></div><p>这是无关的侧栏推荐新闻内容，不应写入新闻正文。</p></div>'
        item = extract_article(raw, 'https://www.chinanews.com/sh/2015/01-02/6929986.shtml', task)
        self.assertEqual(item['publishedAt'], '2015-01-01T21:22:31+00:00')
        self.assertEqual(item['content'], body)
        self.assertNotIn('侧栏', item['content'])

    def test_legacy_cntv_unset_script_date_uses_visible_publication_time(self):
        task = validate_task(config(eventName='上海外滩跨年夜', regionKeyword='上海', keywords=['外滩'],
            timeRange=['2014-12-01 00:00', '2015-01-31 23:59']))
        raw = '<script>var publishDate="19700101080000 ";</script><span class="info">中国广播网 2015年01月02日 09:48</span><h1>上海外滩跨年夜</h1><article><p>' + '上海外滩跨年夜人群安全报道。' * 15 + '</p></article>'
        item = extract_article(raw, 'https://news.cntv.cn/2015/01/02/ARTI1420163280740476.shtml', task)
        self.assertEqual(item['publishedAt'], '2015-01-02T01:48:00+00:00')

    def test_mongo_naive_dates_are_returned_with_utc_offset(self):
        from datetime import datetime
        result = NewsRepository.public({'_id': 'test', 'createdAt': datetime(2026, 10, 9, 11, 47)})
        self.assertEqual(result['createdAt'], '2026-10-09T11:47:00+00:00')

    def test_optional_crawler_exit_does_not_stop_sumo_supervisor(self):
        from unittest.mock import Mock, patch
        from start_backend import Supervisor
        supervisor = Supervisor()
        dead = Mock(pid=1, returncode=1)
        dead.poll.return_value = 1
        supervisor.children = [('新闻采集服务', dead, False)]
        supervisor.optional_names.add('新闻采集服务')
        with patch('start_backend.report'):
            supervisor.check_children()
        self.assertEqual(supervisor.unavailable_services, {'新闻采集服务'})
        self.assertEqual(supervisor.children, [])


class NewsRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = NewsRepository(news_database())
        self.repository.ensure_indexes()

    def claim(self, **changes):
        task = self.repository.create(config(**changes))
        return self.repository.claim('owner', task['id'])

    def test_region_must_be_saved_in_environment_library(self):
        with self.assertRaisesRegex(ValueError, '环境库'):
            self.repository.create(config(regionId='unsaved'))
        self.repository.environments.insert_one({'_id': 'new-region', 'locationLabel': '新建地区', 'is_deleted': False})
        task = self.repository.create(config(regionId='new-region', location='伪造地区'))
        self.assertEqual(task['location'], '新建地区')
        self.repository.environments.update_one({'_id': 'new-region'}, {'$set': {'is_deleted': True}})
        with self.assertRaisesRegex(ValueError, '环境库'):
            self.repository.create(config(regionId='new-region'))
        self.assertEqual(self.repository.get(task['id'])['maxRecords'], 2)

    def test_backfill_persists_locations_without_changing_content_or_tasks(self):
        task = self.claim(regionId='zhengzhou-portrait-region', regionKeyword='郑州', location='郑州')
        original = extract_article(html(title='郑州暴雨河医院区救援', region='郑州'), 'https://example.com/hospital', task)
        for key in ('locationPoints', 'locationPrecision', 'locationVersion'):
            original.pop(key)
        self.assertEqual(self.repository.save_article(task, 'owner', original), 'saved')
        before = self.repository.articles.find_one()
        task_before = self.repository.tasks.find_one()
        self.assertEqual(self.repository.refresh_locations(task['_id']), 1)
        after = self.repository.articles.find_one()
        for key in before:
            self.assertEqual(before[key], after[key])
        self.assertEqual(self.repository.tasks.find_one(), task_before)
        self.assertEqual(self.repository.news(task['_id'])['items'][0]['locationPoints'][0]['name'], '郑大一附院河医院区')
        self.assertEqual(self.repository.refresh_locations(task['_id']), 0)
        with self.assertRaises(KeyError):
            self.repository.refresh_locations('missing')

    def test_distinct_tasks_can_be_claimed_and_expired_lease_is_recovered(self):
        first = self.claim()
        second = self.repository.create(config(eventName='第二案件'))
        claimed = self.repository.claim('second')
        self.assertEqual(claimed['_id'], second['id'])
        self.assertIsNone(self.repository.claim('third'))
        self.repository.tasks.update_one({'_id': first['_id']}, {'$set': {'leaseUntil': now() - timedelta(seconds=1)}})
        reclaimed = self.repository.claim('third')
        self.assertEqual(reclaimed['_id'], first['_id'])
        self.assertEqual(reclaimed['attempts'], 2)
        self.assertFalse(self.repository.heartbeat(first['_id'], 'owner'))
        self.assertEqual(self.repository.save_article(first, 'owner', article(first)), 'stopped')
        self.assertEqual(self.repository.finish(first, 'owner', 'completed', ''), 0)

    def test_url_title_and_content_are_independently_unique_per_event(self):
        task = self.claim()
        original = article(task)
        self.assertEqual(self.repository.save_article(task, 'owner', original), 'saved')
        variants = [dict(original, title='另一条标题', content='不同正文', url=original['url']),
                    dict(original, url='https://example.com/2', content='另一篇正文'),
                    dict(original, url='https://example.com/3', title='转发标题')]
        for value in variants:
            self.assertEqual(self.repository.save_article(task, 'owner', value), 'duplicate')
        other = self.claim(eventName='其它洪涝案件')
        self.assertEqual(self.repository.save_article(other, 'owner', original), 'saved')

    def test_cancellation_prevents_more_writes(self):
        task = self.claim()
        self.repository.cancel(task['_id'])
        self.assertFalse(self.repository.heartbeat(task['_id'], 'owner'))
        self.assertEqual(self.repository.save_article(task, 'owner', article(task)), 'stopped')
        self.assertEqual(self.repository.get(task['_id'])['status'], 'cancelled')

    def test_append_keeps_event_news_and_expands_time_window(self):
        task = self.claim(maxRecords=1)
        self.repository.save_article(task, 'owner', article(task))
        second = dict(article(task, 'https://example.com/2', '成都暴雨第二条'), publishedAt='2021-07-22T00:00:00+00:00')
        self.repository.save_article(task, 'owner', second)
        self.assertEqual(self.repository.news(task['_id'])['total'], 1)
        self.assertEqual(self.repository.news(task['_id'], 2, 1)['items'], [])
        later = self.repository.create(config(timeRange=['2022-01-01', '2022-01-02']))
        self.assertEqual(later['id'], task['_id'])
        self.assertEqual(later['maxRecords'], 4)
        self.assertEqual(later['timeRange'], [task['timeRange'][0], '2022-01-01T16:00:00+00:00'])
        self.assertEqual(self.repository.news(later['id'])['total'], 2)
        self.assertEqual(self.repository.list()['total'], 1)
        other = self.repository.create(config(eventName='另一场暴雨', timeRange=['2022-01-01', '2022-01-02']))
        self.assertEqual(self.repository.news(other['id'])['total'], 0)
        result = self.repository.news(later['id'])['items'][0]
        self.assertNotIn('content', result)
        self.assertNotIn('leaseOwner', self.repository.get(task['_id']))

    def test_simultaneous_submissions_add_to_one_goal(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.repository.create(config(maxRecords=5)), range(16)))
        self.assertEqual(len({task['id'] for task in results}), 1)
        self.assertEqual(self.repository.list()['total'], 1)
        self.assertEqual(self.repository.get(results[0]['id'])['maxRecords'], 80)
        self.assertEqual(sum(task['operation'] == 'created' for task in results), 1)

    def test_append_during_running_crawl_cannot_finish_with_old_target(self):
        task = self.claim(maxRecords=1)
        self.repository.save_article(task, 'owner', article(task))
        appended = self.repository.create(config(maxRecords=5))
        self.assertEqual(appended['id'], task['_id'])
        self.assertEqual(appended['maxRecords'], 6)
        self.assertEqual(appended['status'], 'running')
        self.assertTrue(self.repository.heartbeat(task['_id'], 'owner'))
        self.assertIsNone(self.repository.claim('other'))
        self.assertEqual(self.repository.finish(task, 'owner', 'completed', 'old goal'), 0)
        next_run = self.repository.claim('other')
        self.assertEqual(next_run['maxRecords'], 6)
        self.assertEqual(self.repository.finish(task, 'owner', 'completed', ''), 0)
        self.assertEqual(self.repository.get(task['_id'])['status'], 'running')

    def test_deleted_task_is_hidden_stops_worker_and_recreation_appends(self):
        task = self.claim(maxRecords=5)
        self.repository.save_article(task, 'owner', article(task))
        self.repository.delete(task['_id'])
        self.assertIsNone(self.repository.get(task['_id']))
        self.assertEqual(self.repository.list()['total'], 0)
        self.assertEqual(self.repository.list()['pendingCount'], 0)
        self.assertIsNone(self.repository.claim('next'))
        self.assertFalse(self.repository.heartbeat(task['_id'], 'owner'))
        self.assertEqual(self.repository.save_article(task, 'owner', article(task)), 'stopped')
        self.assertEqual(self.repository.finish(task, 'owner', 'completed', ''), 0)
        self.assertEqual(self.repository.articles.count_documents({}), 1)
        restored = self.repository.create(config(maxRecords=2))
        self.assertEqual(restored['id'], task['_id'])
        self.assertEqual(restored['maxRecords'], 3)
        self.assertEqual(restored['status'], 'queued')
        self.assertEqual(self.repository.news(restored['id'])['total'], 1)

    def test_append_racing_worker_finish_retries_using_terminal_count(self):
        from unittest.mock import patch
        task = self.claim(maxRecords=5)
        self.repository.save_article(task, 'owner', article(task))
        original = self.repository.tasks.find_one
        finished = False
        def read_with_finish(query, *args, **kwargs):
            nonlocal finished
            result = original(query, *args, **kwargs)
            if query.get('canonical') is True and not finished:
                finished = True
                self.repository.finish(task, 'owner', 'partial', 'only one available')
            return result
        with patch.object(self.repository.tasks, 'find_one', side_effect=read_with_finish):
            appended = self.repository.create(config(maxRecords=2))
        self.assertEqual(appended['maxRecords'], 3)
        self.assertEqual(appended['status'], 'queued')
        self.assertNotIn('leaseOwner', self.repository.tasks.find_one({'_id': task['_id']}))
        self.assertIsNotNone(self.repository.claim('next'))

    def test_legacy_duplicates_merge_and_keep_cancellation_and_aliases(self):
        first = self.repository.create(config(maxRecords=5))
        task = self.repository.claim('owner', first['id'])
        self.repository.save_article(task, 'owner', article(task))
        self.repository.tasks.update_one({'_id': first['id']}, {'$unset': {'canonical': '', 'revision': '', 'lastBatchSize': ''}, '$set': {'status': 'completed'}})
        duplicate = self.repository.tasks.find_one({'_id': first['id']})
        duplicate.update(_id='legacy-second', createdAt=now() + timedelta(seconds=1), status='cancelled', maxRecords=100)
        self.repository.tasks.insert_one(duplicate)
        self.repository.consolidate_tasks()
        self.assertEqual(self.repository.list()['total'], 1)
        merged = self.repository.get('legacy-second')
        self.assertEqual(merged['id'], first['id'])
        self.assertEqual(merged['maxRecords'], 105)
        self.assertEqual(merged['status'], 'cancelled')
        self.assertEqual(self.repository.news('legacy-second')['total'], 1)
        self.repository.consolidate_tasks()
        self.assertEqual(self.repository.get(first['id'])['maxRecords'], 105)

    def test_similar_event_spelling_reuses_same_task(self):
        first = self.repository.create(config(eventName='成都“7·20”暴雨', maxRecords=2))
        appended = self.repository.create(config(eventName=' 成都 7.20 暴雨 ', maxRecords=3))
        self.assertEqual(first['id'], appended['id'])
        self.assertEqual(appended['maxRecords'], 5)



class NewsWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_jobs_run_concurrently_and_persist_separately(self):
        repository = NewsRepository(news_database())
        for i in range(3):
            repository.create(config(eventName=f'案件{i}', maxRecords=1, seedUrls=[f'https://example.com/{i}']))
        class Sources:
            active = 0
            peak = 0
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def discover(self, task): return [], []
            async def fetch(self, url):
                self.active += 1
                self.peak = max(self.peak, self.active)
                await asyncio.sleep(.03)
                self.active -= 1
                return html(title='成都暴雨' + url[-1]), url
        sources = Sources()
        await run_worker(repository, concurrency=2, once=True, sources_factory=lambda: sources)
        self.assertEqual(sources.peak, 2)
        self.assertEqual(repository.articles.count_documents({}), 3)
        self.assertEqual({item['status'] for item in repository.list()['items']}, {'completed'})
        self.assertEqual(repository.list()['workerCount'], 0)

    async def test_failure_and_partial_results_are_honest(self):
        repository = NewsRepository(news_database())
        failed = repository.create(config(eventName='无来源案件'))
        partial = repository.create(config(seedUrls=['https://example.com/1']))
        class Sources:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def discover(self, task): return [], ['搜索未返回结果']
            async def fetch(self, url): return html(), url
        await run_worker(repository, once=True, concurrency=2, sources_factory=Sources)
        self.assertEqual(repository.get(failed['id'])['status'], 'failed')
        self.assertEqual(repository.get(partial['id'])['status'], 'partial')
        self.assertEqual(repository.get(partial['id'])['recordCount'], 1)
        self.assertEqual(repository.get(partial['id'])['sourceErrors'], ['搜索未返回结果'])

    async def test_completed_task_submission_collects_additional_unique_news(self):
        repository = NewsRepository(news_database())
        saved = repository.create(config(maxRecords=1, seedUrls=['https://example.com/first']))
        task = repository.claim('first', saved['id'])
        repository.save_article(task, 'first', article(task, 'https://example.com/first'))
        repository.finish(task, 'first', 'completed', '')
        second = repository.create(config(maxRecords=1, seedUrls=['https://example.com/additional']))
        class Sources:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def discover(self, task): return [], []
            async def fetch(self, url):
                return html(title='成都暴雨追加报道' if url.endswith('additional') else '成都暴雨救援'), url
        await run_worker(repository, once=True, sources_factory=Sources)
        self.assertEqual(second['id'], saved['id'])
        self.assertEqual(repository.get(second['id'])['status'], 'completed')
        self.assertEqual(repository.articles.count_documents({}), 2)
        self.assertEqual(repository.list()['total'], 1)


class NewsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        from unittest.mock import AsyncMock
        self.model = AsyncMock()
        self.model.model = 'test-model'
        self.model.extract.return_value = {'keywords': ['成都', '暴雨', '抢险救援']}
        self.repository = register_news_routes(app, news_database(), model=self.model)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.base = '/api/crowdSim/news-collection/tasks'

    async def asyncTearDown(self):
        await self.client.close()

    async def test_task_creation_read_news_and_cancel(self):
        response = await self.client.post(self.base, json=config())
        self.assertEqual(response.status, 201)
        task = (await response.json())['data']
        self.assertEqual(task['status'], 'queued')
        response = await self.client.get(self.base)
        listing = (await response.json())['data']
        self.assertEqual(listing['total'], 1)
        self.assertEqual(listing['pendingCount'], 1)
        response = await self.client.get(f"{self.base}/{task['id']}/news")
        self.assertEqual((await response.json())['data']['items'], [])
        response = await self.client.post(f"{self.base}/{task['id']}/cancel")
        self.assertEqual((await response.json())['data']['status'], 'cancelled')
        self.assertEqual((await self.client.get(self.base + '/missing')).status, 404)

    async def test_model_keywords_use_saved_region_and_do_not_create_tasks(self):
        payload = {**config(), 'location': '伪造地区', 'excludedKeywords': ['成都']}
        response = await self.client.post('/api/crowdSim/news-collection/recommend-keywords', json=payload)
        self.assertEqual(response.status, 200)
        result = (await response.json())['data']
        self.assertEqual(result, {'keywords': ['暴雨', '抢险救援'], 'source': 'llm', 'model': 'test-model'})
        import json
        context = json.loads(self.model.extract.call_args.args[1])
        self.assertEqual(context['region']['locationLabel'], '成都 · 中国')
        self.assertNotIn('location', context)
        self.assertEqual(self.model.extract.call_args.kwargs, {'fast_json': True})
        self.assertEqual(self.repository.list()['total'], 0)
        self.model.extract.reset_mock()
        response = await self.client.post('/api/crowdSim/news-collection/recommend-keywords', json={**payload, 'regionId': 'missing'})
        self.assertEqual(response.status, 400)
        self.model.extract.assert_not_called()

    async def test_model_failure_and_bad_output_do_not_become_fake_suggestions(self):
        self.model.extract.side_effect = ModelError('模型不可用')
        response = await self.client.post('/api/crowdSim/news-collection/recommend-keywords', json=config())
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())['message'], '模型不可用')
        self.model.extract.side_effect = None
        for value in ({'keywords': '暴雨'}, {'keywords': ['成都']}, {'keywords': ['x']}, {'keywords': []}):
            with self.subTest(value=value):
                self.model.extract.return_value = value
                response = await self.client.post('/api/crowdSim/news-collection/recommend-keywords', json=config())
                self.assertEqual(response.status, 502)

    async def test_disconnected_recommendation_releases_model_work(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.model.extract.side_effect = slow
        pending = asyncio.create_task(self.client.post('/api/crowdSim/news-collection/recommend-keywords', json=config()))
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await asyncio.wait_for(cancelled.wait(), 2)
        self.model.extract.side_effect = None
        response = await self.client.post('/api/crowdSim/news-collection/recommend-keywords', json=config())
        self.assertEqual(response.status, 200)

    async def test_recommendation_context_validation_and_final_edited_tags(self):
        for changes in ({'eventName': ''}, {'timeRange': None}, {'excludedKeywords': ['x']}, {'timeRange': ['2022-01-02', '2022-01-01']}):
            with self.subTest(changes=changes):
                response = await self.client.post('/api/crowdSim/news-collection/recommend-keywords', json=config(**changes))
                self.assertEqual(response.status, 400)
        self.model.extract.assert_not_called()
        self.model.extract.return_value = {'keywords': ['暴雨', ' 暴雨 ', '抢险救援']}
        result = await recommend_keywords(config(), self.repository.environments, self.model)
        self.assertEqual(result['keywords'], ['暴雨', '抢险救援'])
        first = self.repository.create(config(keywords=result['keywords']))
        second = self.repository.create(config(keywords=['抢险救援', '人工新增']))
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['keywords'], ['抢险救援', '人工新增'])

    async def test_repeated_submission_delete_and_recreate_contract(self):
        response = await self.client.post(self.base, json=config(maxRecords=5))
        task = (await response.json())['data']
        response = await self.client.post(self.base, json=config(maxRecords=3))
        self.assertEqual(response.status, 200)
        appended = (await response.json())['data']
        self.assertEqual(appended['id'], task['id'])
        self.assertEqual(appended['maxRecords'], 8)
        self.assertEqual(appended['operation'], 'appended')
        response = await self.client.delete(f"{self.base}/{task['id']}")
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['data']['newsRetained'])
        self.assertEqual((await self.client.get(f"{self.base}/{task['id']}")).status, 404)
        self.assertEqual((await self.client.get(f"{self.base}/{task['id']}/news")).status, 404)
        self.assertEqual((await self.client.get(self.base)).status, 200)
        self.assertEqual((await (await self.client.get(self.base)).json())['data']['total'], 0)
        response = await self.client.post(self.base, json=config(maxRecords=2))
        restored = (await response.json())['data']
        self.assertEqual(restored['id'], task['id'])
        self.assertEqual(restored['maxRecords'], 2)
        self.assertEqual((await self.client.delete(self.base + '/missing')).status, 404)

    async def test_invalid_json_and_pagination(self):
        self.assertEqual((await self.client.post(self.base, data='{')).status, 400)
        self.assertEqual((await self.client.post(self.base, json=config(maxRecords=0))).status, 400)
        self.assertEqual((await self.client.get(self.base + '?page_size=501')).status, 400)


class KeywordModelAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.answer = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"keywords":["暴雨"]}'}}]}

        async def complete(request):
            self.requests.append(await request.json())
            return web.json_response(self.answer)

        app = web.Application()
        app.router.add_post('/chat/completions', complete)
        self.server = TestServer(app)
        await self.server.start_server()
        self.model = object.__new__(JsonModelClient)
        self.model.base_url = str(self.server.make_url('')).rstrip('/')
        self.model.model = 'local-test-model'
        self.model.api_key = ''
        self.model.max_output_tokens = 3000
        self.model.extra = {'chat_template_kwargs': {'enable_thinking': True}}
        await self.model.open()

    async def asyncTearDown(self):
        await self.model.close()
        await self.server.close()

    async def test_tag_budget_stops_reasoning_without_changing_document_defaults(self):
        await self.model.extract('tags', '{}', fast_json=True)
        tags = self.requests[-1]
        self.assertEqual(tags['max_tokens'], 1024)
        self.assertEqual(tags['reasoning_budget_tokens'], 0)
        self.assertFalse(tags['enable_thinking'])
        self.assertFalse(tags['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(tags['response_format'], {'type': 'json_object'})
        await self.model.extract('document', '{}')
        document = self.requests[-1]
        self.assertEqual(document['max_tokens'], 3000)
        self.assertTrue(document['chat_template_kwargs']['enable_thinking'])
        self.assertNotIn('reasoning_budget_tokens', document)

    async def test_complete_tag_json_at_token_limit_is_usable_but_incomplete_answer_is_not(self):
        self.answer['choices'][0]['finish_reason'] = 'length'
        self.assertEqual(await self.model.extract('tags', '{}', fast_json=True), {'keywords': ['暴雨']})
        with self.assertRaisesRegex(ModelError, '截断'):
            await self.model.extract('document', '{}')
        for content in (None, '{"keywords":['):
            self.answer['choices'][0]['message'] = {'content': content, 'reasoning_content': 'unfinished reasoning'}
            with self.assertRaisesRegex(ModelError, '截断'):
                await self.model.extract('tags', '{}', fast_json=True)


if __name__ == '__main__':
    unittest.main()
