"""One persisted task road network shared by governance and forecasting."""
import asyncio
from shapely.geometry import box, shape
from shapely.ops import unary_union
from ..connectors.geography import GeographyConnector
from ..repositories.datasets import DatasetConflictError
from ..repositories.task_roads import TaskRoadRepository
from ..schemas.environments import validate_roads
from .pedestrian_network import BUILDER_VERSION, clip_network, pedestrian


class TaskRoadService:
    def __init__(self, database, environments):
        self.snapshots = TaskRoadRepository(database)
        self.environments = environments
        self.geography = GeographyConnector(database)
        self.lock = asyncio.Lock()

    def environment_roads(self, task):
        binding = task.get('components', {}).get('road_network_binding') or {}
        identifier = binding.get('environment_id') or task['region'].get('environment_id')
        if identifier:
            environment = self.environments.get(identifier)
            revision = binding.get('environment_revision') or task['region'].get('environment_revision')
            if environment is None or environment['revision'] != revision:
                archived = self.environments.road_versions.get(identifier, revision)
                if not archived:
                    raise DatasetConflictError('所引用环境库路网版本暂不可读，请重新选择区域版本')
                environment = {**(environment or {}), 'id': identifier, 'revision': revision,
                               'title': task['region']['name'], 'geometry': archived['boundary']}
            candidates = [environment]
        else:
            candidates = self.environments.collection.find({'is_deleted': {'$ne': True}, 'roadsCount': {'$gt': 0}})
        boundary = shape(task['region']['geometry'])
        matched_features, sources, coverages = {}, [], []
        auxiliary = {key: [] for key in ('walkable_areas', 'obstacles', 'barriers', 'entrances')}
        evidence, access_audits, simulation_networks = [], [], []
        candidates = sorted(candidates, key=lambda item: (shape(item['geometry']).area, item.get('id') or item['_id']))
        for candidate in candidates:
            # Nearby resources may include roads beyond their own park boundary.
            env_id = candidate.get('id') or candidate['_id']
            env_shape = shape(candidate['geometry'])
            if not env_shape.intersects(boundary):
                continue
            roads = self.environments.open_roads(env_id, revision=candidate['revision'])
            matched = [f for f in roads.get('features', []) if pedestrian(f) and shape(f['geometry']).intersects(boundary)]
            if not matched:
                continue
            bbox = roads.get('source', {}).get('bbox')
            coverage = box(*bbox) if roads.get('source', {}).get('scope') != 'boundary_clipped' and isinstance(bbox, list) and len(bbox) == 4 else env_shape
            fraction = coverage.intersection(boundary).area / boundary.area
            contains_environment = boundary.buffer(1e-8).covers(env_shape)
            if not identifier and fraction < .8 and not contains_environment:
                continue
            sources.append({'environment_id': env_id, 'environment_revision': candidate['revision'],
                            'label': candidate['title'], 'contains_environment': contains_environment,
                            'road_source': roads.get('source', {})})
            coverages.append(coverage)
            for key in auxiliary:
                auxiliary[key].extend(roads.get(key, {}).get('features', []))
            evidence.extend(roads.get('evidence', []))
            if roads.get('simulation_network'):
                simulation_networks.append(roads['simulation_network'])
            if roads.get('access_audit'):
                access_audits.append({'environment_id': env_id, **roads['access_audit']})
            for feature in matched:
                props = feature.get('properties') or {}
                key = ('osm', props['osm_id'], shape(feature['geometry']).wkb) if props.get('osm_id') else ('geometry', shape(feature['geometry']).wkb)
                matched_features.setdefault(key, feature)
        if not sources:
            return None
        fraction = unary_union(coverages).intersection(boundary).area / boundary.area
        source = {'provider': 'environment', 'environments': sources, 'primary_environment_id': sources[0]['environment_id'], 'label': '、'.join(item['label'] for item in sources),
                  'coverage_fraction': round(fraction, 4), 'coordinateSystem': 'WGS84', 'builder_version': BUILDER_VERSION}
        selected = {'type': 'FeatureCollection', 'features': list(matched_features.values()), 'source': source,
                    **{key: {'type': 'FeatureCollection', 'features': value} for key, value in auxiliary.items()}, 'evidence': evidence,
                    'access_audit': {'inventories': access_audits} if access_audits else {},
                    'temporal': {'historical_verified': False, 'note': '以所引用环境库版本为准，历史事件适用性尚待核对。'}}
        # Native SUMO IDs and connections must survive environment-to-task
        # copying; geometry-only reconstruction cannot recover junction links.
        if len(simulation_networks) == 1:
            selected['simulation_network'] = simulation_networks[0]
        if fraction < .999:
            selected['warnings'] = ['已复用环境库步行路网；超出所引用路网覆盖范围的部分尚无完整通道记录，当前仅计算已记录道路。']
        return clip_network(selected, task['region']['geometry']), source

    async def status(self, task):
        # Persist a match once, even before the first forecast. Downstream pages
        # inherit the same snapshot ID rather than doing their own spatial match.
        async with self.lock:
            snapshot = await asyncio.to_thread(self.snapshots.latest, task)
            if snapshot and snapshot.get('source', {}).get('provider') in ('environment', 'task_import'):
                return {'status': 'saved', **snapshot}
            candidate = await asyncio.to_thread(self.environment_roads, task)
            if candidate:
                roads, source = candidate
                snapshot = await asyncio.to_thread(self.snapshots.save, task, roads, source, replace_snapshot=snapshot['id'] if snapshot else None)
                return {'status': 'saved', **snapshot}
            if snapshot:
                return {'status': 'saved', **snapshot}
        return {'status': 'missing', 'feature_count': 0, 'source': {}}

    async def describe(self, task):
        try:
            return await self.status(task)
        except (ValueError, DatasetConflictError, KeyError) as exc:
            return {'status': 'unavailable', 'feature_count': 0, 'source': {}, 'message': str(exc)}

    async def task_context(self, task):
        return {**task, 'road_network': await self.describe(task)}

    async def resolve(self, task, *, refresh=False):
        async with self.lock:
            snapshot = await asyncio.to_thread(self.snapshots.latest, task)
            if snapshot and not refresh and snapshot.get('source', {}).get('provider') in ('environment', 'task_import'):
                return await asyncio.to_thread(self.snapshots.read, snapshot['id']), snapshot
            # Refresh still takes environment-library roads first. It must never
            # silently replace a matched environment network with an OSM query.
            candidate = await asyncio.to_thread(self.environment_roads, task)
            if candidate:
                roads, source = candidate
                if snapshot:
                    saved = await asyncio.to_thread(self.snapshots.read, snapshot['id'])
                    old_source = {key: value for key, value in snapshot['source'].items() if key != 'primary_environment_id'}
                    new_source = {key: value for key, value in source.items() if key != 'primary_environment_id'}
                    asset_keys = ('features', 'warnings', 'walkable_areas', 'obstacles', 'barriers', 'entrances', 'evidence', 'temporal', 'builder_version')
                    if old_source == new_source and all(saved.get(key) == roads.get(key) for key in asset_keys):
                        return saved, snapshot
            else:
                if snapshot and not refresh:
                    return await asyncio.to_thread(self.snapshots.read, snapshot['id']), snapshot
                if snapshot and snapshot.get('source', {}).get('provider') == 'environment':
                    raise ValueError('已关联环境库路网当前不可更新，请先在环境库补充；任务仍保留原路网快照')
                fetched = await self.geography.roads(task['region']['geometry'])
                roads = fetched['roads']
                boundary = shape(task['region']['geometry'])
                roads['features'] = [f for f in roads['features'] if pedestrian(f) and shape(f['geometry']).intersects(boundary)]
                source = {**roads.get('source', {}), 'label': 'OpenStreetMap 任务范围步行路网'}
            if not roads.get('features'):
                raise ValueError('该任务范围暂无可用步行路网；可在环境库补充实测步行通道，或明确勾选区域内部可行走假设')
            snapshot = await asyncio.to_thread(self.snapshots.save, task, roads, source, replace_snapshot=snapshot['id'] if snapshot else None)
            return await asyncio.to_thread(self.snapshots.read, snapshot['id']), snapshot

    async def import_network(self, task, value, filename):
        if isinstance(value, dict) and isinstance(value.get('features'), list):
            for feature in value['features']:
                if isinstance(feature, dict) and feature.get('properties') is None:
                    feature['properties'] = {}
        roads = validate_roads(value)
        boundary = shape(task['region']['geometry'])
        # The import explicitly represents a pedestrian network. Untagged lines
        # are walkways; explicit access restrictions remain authoritative.
        for feature in roads['features']:
            if not isinstance(feature['properties'], dict) or len({tuple(point) for point in feature['geometry']['coordinates']}) < 2:
                raise ValueError('道路属性须为对象，且每条道路至少包含两个不同顶点')
            if not feature['properties'].get('highway') and not feature['properties'].get('foot'):
                feature['properties']['highway'] = 'footway'
        roads['features'] = [feature for feature in roads['features'] if pedestrian(feature) and shape(feature['geometry']).intersects(boundary)]
        if not roads['features']:
            raise ValueError('文件中没有与当前任务范围相交的可步行 LineString 道路')
        source = {'provider': 'task_import', 'label': '任务导入步行路网', 'filename': filename, 'coordinateSystem': 'WGS84'}
        roads['source'] = source
        async with self.lock:
            previous = await asyncio.to_thread(self.snapshots.latest, task)
            snapshot = await asyncio.to_thread(self.snapshots.save, task, roads, source, replace_snapshot=previous['id'] if previous else None)
            saved = await asyncio.to_thread(self.snapshots.read, snapshot['id'])
            if snapshot['source'].get('provider') != 'task_import' or snapshot.get('source') != source or saved['features'] != roads['features']:
                raise DatasetConflictError('路网已被其他请求更新，请重新读取后再导入')
            return saved, snapshot
