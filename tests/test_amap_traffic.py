"""Current traffic provider: missing data, credentials, cached scope and safe errors."""
import logging
import unittest
from unittest.mock import AsyncMock, Mock, patch

from data_service.connectors.amap_traffic import AmapTrafficConnector, normalize, URL, _TrafficRequestLogFilter


class AmapTrafficTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.task = {'time_window': '未来研究时段', 'region': {'geometry': {'type': 'Polygon', 'coordinates': [[[121.48,31.24],[121.49,31.24],[121.49,31.25],[121.48,31.25],[121.48,31.24]]]}}}
        self.connector = AmapTrafficConnector()

    async def test_missing_key_has_no_request_or_fabricated_zero(self):
        with patch.dict('os.environ', {'CROWDSIM_AMAP_WEB_KEY': ''}), patch('httpx.AsyncClient') as client:
            result = await self.connector.read(self.task)
            client.assert_not_called()
        self.assertEqual(result['status'], 'unconfigured')
        self.assertIsNone(result['vehicle_count'])
        self.assertIsNone(result['fetched_at'])

    async def test_cache_keeps_query_scope_and_does_not_attach_wrong_task_time(self):
        response = Mock()
        response.json.return_value = {'status':'1', 'trafficinfo': {'roads': [{'name':'中山东一路','status':'2','speed':'23','polyline':'121.48,31.24;121.49,31.25'}]}}
        client = AsyncMock()
        client.get.return_value = response
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        with patch.dict('os.environ', {'CROWDSIM_AMAP_WEB_KEY': 'test-key', 'CROWDSIM_AMAP_WEB_PRIVATE_KEY': ''}), patch('httpx.AsyncClient', return_value=manager):
            first = await self.connector.read(self.task)
            second = await self.connector.read({**self.task, 'time_window':'另一个研究时间'})
            self.assertEqual(client.get.await_count, 1)
            await self.connector.read(self.task, 500)
            self.assertEqual(client.get.await_count, 2)
        self.assertEqual(first['roads'][0]['speed_kmh'], 23)
        self.assertEqual(first['roads'][0]['status_label'], '缓行')
        self.assertEqual(second['task_time_window'], '另一个研究时间')
        self.assertIsNone(first['source_observed_at'])
        self.assertIsNotNone(first['fetched_at'])
        self.assertNotIn('test-key', str(first))
        self.assertEqual(client.get.call_args.kwargs['params']['radius'], 500)

    async def test_denied_and_malformed_response_remain_unavailable(self):
        for value in [{'status':'0','infocode':'10009','info':'secret-key'}, {'status':'1'}]:
            response = Mock(); response.json.return_value = value
            client = AsyncMock(); client.get.return_value = response
            manager = AsyncMock(); manager.__aenter__.return_value = client
            self.connector.cache.clear()
            with patch.dict('os.environ', {'CROWDSIM_AMAP_WEB_KEY': 'secret-key'}), patch('httpx.AsyncClient', return_value=manager):
                result = await self.connector.read(self.task)
            self.assertEqual(result['status'], 'unavailable')
            self.assertNotIn('secret-key', str(result))
            self.assertIsNone(result['pedestrian_count'])

    def test_missing_values_unknown_roads_and_credentials_in_logs(self):
        result = normalize({'trafficinfo': {'evaluation': {'expedite':'98%'}, 'roads':[{'name':'无速度道路','status':'0','speed':[]}, {'name':'无效值','status':'NaN','speed':'NaN','polyline':'bad'}]}})
        self.assertEqual(result['evaluation']['expedite'], 98)
        self.assertIsNone(result['evaluation']['blocked'])
        self.assertTrue(all(r['speed_kmh'] is None and r['status_label'] == '未知' for r in result['roads']))
        record = logging.LogRecord('httpx', logging.INFO, '', 1, 'HTTP Request: GET %s', (URL+'?key=secret',), None)
        self.assertFalse(_TrafficRequestLogFilter().filter(record))
