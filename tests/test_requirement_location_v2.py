"""Addressed chat snapshots: no model, network service or SUMO process needed."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from crowdsim.domain.requirement_spec import RequirementSpec, RequirementValidationError
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.websocket_server import OverlayServer, CommandError
from tests.test_requirement_submission import requirement_payload


def located_requirement(memorial=False):
    raw = requirement_payload()
    raw['schema_version'] = 2
    scope = raw['spatial_scope']
    scope.update(source='named', location_id='amap:test-poi', name='杭州西湖', address='浙江省杭州市西湖区西湖景区', center=[120.14, 30.24], boundary=None)
    if memorial:
        preset = json.loads((Path(__file__).resolve().parents[1] / 'config/during_presets/memorial_tower_v1.json').read_text())
        raw['spatial_scope'] = scope = deepcopy(preset['requirement']['spatial_scope'])
    scope['location_resolution'] = {
        'provider': 'amap', 'confirmed': True, 'confirmed_at': '2026-10-15T00:00:00Z', 'queried_at': '2026-10-15T00:00:00Z',
        'original_query': scope['name'], 'name': scope['name'], 'address': scope['address'], 'poi_id': 'test-poi',
        'crs': 'EPSG:4326', 'center': scope['center'][:], 'provider_crs': 'GCJ-02', 'provider_center': [121.49, 31.24],
        'adcode': '310101' if memorial else '330106',
    }
    return raw


class LocationRequirementTests(unittest.IsolatedAsyncioTestCase):
    def test_point_requirement_round_trips_without_polygon_and_retains_address(self):
        raw = located_requirement()
        spec = RequirementSpec.parse(raw)
        self.assertEqual('not_implemented', spec.capabilities()['location'])
        self.assertEqual('supported', spec.capabilities()['map_location'])
        with tempfile.TemporaryDirectory() as directory:
            repository = RequirementRepository(directory)
            saved = repository.create(spec)
            self.assertEqual(spec.payload, repository.load(saved['requirement_id'])['requirement'])
            self.assertIsNone(saved['requirement']['spatial_scope']['boundary'])

    def test_memorial_keeps_exact_known_boundary_and_can_match_scene(self):
        raw = located_requirement(True)
        self.assertEqual('supported', RequirementSpec.parse(raw).capabilities()['location'])
        raw['spatial_scope']['boundary']['coordinates'][0][0][0] += .01
        with self.assertRaises(RequirementValidationError):
            RequirementSpec.parse(raw)

    def test_invented_or_unconfirmed_location_and_unknown_crs_are_rejected(self):
        for field, value in [('confirmed', False), ('provider', 'model'), ('address', '另一个地址'), ('crs', 'unknown'), ('provider_center', [None, 30])]:
            with self.subTest(field=field):
                raw = located_requirement()
                raw['spatial_scope']['location_resolution'][field] = value
                with self.assertRaises(RequirementValidationError):
                    RequirementSpec.parse(raw)

    def test_other_place_cannot_inherit_memorial_boundary_or_become_a_scene_by_name(self):
        raw = located_requirement()
        raw['spatial_scope']['boundary'] = located_requirement(True)['spatial_scope']['boundary']
        with self.assertRaises(RequirementValidationError):
            RequirementSpec.parse(raw)
        raw = located_requirement(True)
        raw['spatial_scope']['location_resolution']['adcode'] = '320101'
        with self.assertRaises(RequirementValidationError):
            RequirementSpec.parse(raw)

    def test_v1_contract_and_locations_are_unchanged(self):
        raw = requirement_payload('east-nanjing-road')
        self.assertEqual('supported', RequirementSpec.parse(raw).capabilities()['location'])
        raw['spatial_scope']['boundary'] = None
        with self.assertRaises(RequirementValidationError):
            RequirementSpec.parse(raw)
        raw = located_requirement()
        raw['spatial_scope']['location_id'] = 'east-nanjing-road'
        self.assertEqual('not_implemented', RequirementSpec.parse(raw).capabilities()['location'])

    def test_retries_and_concurrent_saves_are_idempotent_but_changed_addresses_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = RequirementRepository(directory)
            spec = RequirementSpec.parse(located_requirement())
            with ThreadPoolExecutor(max_workers=4) as pool:
                records = list(pool.map(lambda _: repository.create(spec), range(8)))
            self.assertEqual(1, len({r['requirement_id'] for r in records}))
            self.assertEqual(1, len(list(Path(directory).glob('*.json'))))
            raw = located_requirement()
            raw['spatial_scope']['address'] = raw['spatial_scope']['location_resolution']['address'] = '浙江省杭州市西湖区另一入口'
            different = repository.create(RequirementSpec.parse(raw))
            self.assertNotEqual(records[0]['requirement_id'], different['requirement_id'])

    async def test_submission_only_saves_and_non_memorial_cannot_initialize(self):
        with tempfile.TemporaryDirectory() as directory:
            server = OverlayServer(SimpleNamespace(), '127.0.0.1', 0)
            server.requirements = RequirementRepository(directory)
            server._send = AsyncMock()
            await server._submit_requirement({'requirement': located_requirement()}, 'request-test')
            accepted = server._send.call_args.args[0]
            self.assertEqual('requirement_accepted', accepted['type'])
            with self.assertRaises(CommandError) as caught:
                await server._configure({'requirement_id': accepted['requirement_id']}, 'configure-test')
            self.assertEqual('unsupported_requirement_location', caught.exception.code)

    def test_exact_park_address_accepts_other_poi_names_with_the_same_preset_geometry(self):
        address = '中山东一路475号黄浦公园(苏州河口南侧的黄浦江西岸)'
        for formatted in (address, address.replace('(', '（').replace(')', '）'),
                          '上海市黄浦区' + address,
                          ' 上海市 黄浦区 中山东一路 ４７５号 黄浦公园 （苏州河口南侧的黄浦江西岸） '):
            with self.subTest(address=formatted):
                raw = located_requirement(True)
                scope = raw['spatial_scope']
                scope['name'] = scope['location_resolution']['name'] = '黄浦公园园区'
                scope['address'] = scope['location_resolution']['address'] = formatted
                scope['location_resolution']['adcode'] = ''
                scope['location_resolution']['center'] = [121.48, 31.24]
                spec = RequirementSpec.parse(raw)
                self.assertEqual('supported', spec.capabilities()['location'])
                with tempfile.TemporaryDirectory() as directory:
                    repository = RequirementRepository(directory)
                    saved = repository.create(spec)
                    self.assertEqual(spec.payload, repository.load(saved['requirement_id'])['requirement'])
                scope['boundary']['coordinates'][0][0][0] += .01
                with self.assertRaises(RequirementValidationError):
                    RequirementSpec.parse(raw)

    def test_nearby_or_partial_addresses_do_not_bind_the_preset_and_confirmation_remains_required(self):
        address = '中山东一路475号黄浦公园(苏州河口南侧的黄浦江西岸)'
        for nonmatching in (address.replace('475', '476'), address + '附近', '黄浦公园',
                            address.replace('黄浦公园', '黄埔公园'), '杭州市' + address):
            raw = located_requirement(True)
            scope = raw['spatial_scope']
            scope['name'] = scope['location_resolution']['name'] = '黄浦公园园区'
            scope['address'] = scope['location_resolution']['address'] = nonmatching
            with self.subTest(address=nonmatching), self.assertRaises(RequirementValidationError):
                RequirementSpec.parse(raw)
        for field, value in (('confirmed', False), ('provider', 'model')):
            raw = located_requirement(True)
            scope = raw['spatial_scope']
            scope['name'] = scope['location_resolution']['name'] = '黄浦公园园区'
            scope['address'] = scope['location_resolution']['address'] = address
            scope['location_resolution'][field] = value
            with self.subTest(field=field), self.assertRaises(RequirementValidationError):
                RequirementSpec.parse(raw)


if __name__ == '__main__':
    unittest.main()
