"""Evidence-backed site, crowd and activity model for every saved task.

Opening a model is read-only: legacy tasks keep their revisions and run snapshots.
Only an explicit edit persists the portrait and any edited common scenario inputs.
"""
import copy
import json
from pathlib import Path

from shapely.geometry import Point, shape

from ..prediction.inputs import default_settings, normalize_settings
from ..repositories.analysis_tasks import AnalysisTaskRepository
from ..repositories.datasets import DatasetConflictError
from ..schemas.environments import coordinate
from . import domain_demand, domain_relations

DEFINITION = '有证据支持的场地、人群与活动关系模型'
TASK_FIELDS = ('title', 'summary', 'event_id', 'event_label', 'time_window', 'template_case', 'status', 'region', 'population', 'references', 'components')
COMMON_FIELDS = ('initial_crowd', 'arrival_rate_per_minute', 'dwell_minutes', 'free_speed_mps', 'path_width_m', 'warning_density_p_m2',
                 'attraction_point', 'exit_points', 'exit_labels', 'exit_points_note', 'entrance_shares')
EDITABLE_SETTINGS = COMMON_FIELDS[:6]
RESOURCE = Path(__file__).resolve().parents[1] / 'resources/domain_portrait/monument.json'


def _point(identifier, title, coordinates, category='access', **extra):
    return dict(id=identifier, title=title, coordinates=coordinates, category=category,
                category_label='场景接入' if category == 'access' else '研究关注', road_ids=[],
                status='任务设定 · 待核对', fact='当前任务中保存的位置。', inference='现场用途与活动方式需要资料支持。',
                chain=['任务保存的位置', '核对空间与活动关系'], needed=['现场用途、位置和适用时段'],
                location_note='来自当前任务，不能自动视为实测位置。', evidence_ids=['scope'], **extra)


def build_portrait(task, roads=None, metadata=None, news=()):
    components = task.get('components', {})
    previous = copy.deepcopy(components.get('domain_portrait') or {})
    region = task['region']
    boundary = shape(region['geometry'])
    bounds = boundary.bounds
    # A shared template_case alone is not evidence that this is the same site.
    monument = boundary.intersects(shape(json.loads(RESOURCE.read_text())['site_geometry'])) if RESOURCE.exists() else False
    monument = monument and bounds[2] - bounds[0] < .03 and bounds[3] - bounds[1] < .03
    seed = json.loads(RESOURCE.read_text()) if monument else {}
    binding = components.get('road_network_binding') or region
    source = (metadata or {}).get('source', {}).get('environments', [])
    revision = source[0].get('environment_revision') if source else binding.get('environment_revision')
    evidence = [
        dict(id='boundary', title='研究边界', kind='场地资料', source=region.get('name', '当前任务'),
             content='当前任务保存的研究边界；边界面积不等于净可行走面积。', dimensions=['space'],
             url=region.get('boundary_source', {}).get('url', ''), date=''),
        dict(id='roads', title='场地路网', kind='场地资料', source=f'任务引用路网 / 环境版本 {revision or "待关联"}',
             content='路网与实验使用同一任务引用。通道宽度、用途与开放条件需按来源核对。', dimensions=['space', 'access'], date=''),
        dict(id='scope', title='研究场景与时间', kind='任务设定', source='当前分析任务', date=task.get('time_window', ''),
             content=f'{task.get("event_label") or "研究场景"}；{task.get("time_window") or "时间待补充"}。任务设定不等于活动公告。', dimensions=['demand', 'people']),
    ]
    merged_evidence = {row['id']: row for row in evidence + seed.get('evidence', []) + previous.get('evidence', [])}
    # Scope always belongs to the task being opened, including legacy preview tasks.
    merged_evidence['scope'] = evidence[-1]
    merged_evidence['amap_traffic'] = dict(id='amap_traffic', title='高德周边实时道路路况', kind='在线数据入口', source='高德地图官方交通图层 / 交通态势查询',
        url='https://lbs.amap.com/api/javascript-api-v2/guide/layers/official-layers', date='当前时刻 · 地图另标注读取时间', dimensions=['access'],
        content='提供周边道路拥堵颜色；开通 Web 交通态势查询后可取得路段平均速度、拥堵比例与坐标。',
        scope='当前研究场地周边 500 米、1 公里或 2 公里', applicability='用于观察到达条件；车辆路况和到访人次分别记录。')
    candidates = {row['id']: row for row in seed.get('candidates', []) + previous.get('candidates', [])}
    visualizations = {**copy.deepcopy(seed.get('visualizations', {})), **previous.get('visualizations', {})}
    visualizations['influence_clues'] = domain_relations.clue_input_relations(visualizations.get('influence_clues', {}))
    features = {str(f.get('properties', {}).get('sumo_edge_id')): f for f in (roads or {}).get('features', [])}
    points = []
    for row in visualizations.get('spatial_points', []):
        row = copy.deepcopy(row)
        anchor = row.get('anchor')
        if anchor:
            feature = features.get(anchor['edge_id'])
            if feature and feature.get('geometry', {}).get('type') == 'LineString':
                coords = feature['geometry']['coordinates']
                row['coordinates'] = max(coords, key=lambda p: p[0]) if anchor['method'] == 'eastmost' else [(coords[0][i] + coords[-1][i]) / 2 for i in (0, 1)]
                row['coordinate_source'] = dict(environment_revision=revision, edge_id=anchor['edge_id'], crs='WGS84')
            elif roads is not None or row.get('coordinate_source', {}).get('environment_revision') != revision:
                continue
        if row.get('coordinates') and boundary.buffer(.000005).covers(Point(row['coordinates'])):
            points.append(row)
    old_scene = previous.get('scene') or {}
    try:
        settings = default_settings(task)
    except ValueError:
        settings = copy.deepcopy(components.get('forecast_setup', {}))
    settings = {k: v for k, v in settings.items() if k in COMMON_FIELDS}
    # Carry the approved preview's edits forward once. Formal models read the
    # canonical scenario, including edits subsequently made in experiment control.
    preview = previous.get('mode') == 'preview'
    if preview:
        settings.update(old_scene.get('settings', {}))
    population = copy.deepcopy(old_scene.get('population', task.get('population', {})) if preview else task.get('population', {}))
    for identifier, coords in settings.get('exit_points', {}).items():
        if not any(Point(row['coordinates']).distance(Point(coords)) < .00002 for row in points):
            points.append(_point('exit_' + identifier, settings.get('exit_labels', {}).get(identifier, '场景通道 ' + identifier), coords))
    if not points and settings.get('attraction_point'):
        points.append(_point('target', '任务聚集位置', settings['attraction_point'], 'attraction'))
    for row in components.get('domain_knowledge', {}).get('annotations', []):
        identifier = 'annotation_' + row['id']
        if not any(point['id'] == identifier for point in points):
            point = _point(identifier, row['title'], row['coordinates'], 'passage')
            point.update(fact=row.get('source_note') or '任务保存的空间标注，来源待补充。',
                         inference=row.get('description') or '活动关系待补充。', status={'verified': '已核对', 'assumed': '情景假设'}.get(row['status'], '待核对'))
            points.append(point)
    for i, row in enumerate(points):
        row['number'] = str(i + 1)
        row.setdefault('road_ids', [])
    visualizations['spatial_points'] = points
    portrait = {**previous, 'schema_version': 2, 'mode': 'formal', 'title': '人群聚集领域模型', 'definition': DEFINITION,
                'evidence': list(merged_evidence.values()), 'candidates': list(candidates.values()), 'visualizations': visualizations,
                'overview': previous.get('overview') or seed.get('overview') or {'label': '场地与任务资料 · 观测待补充'}}
    portrait['scene'] = {**old_scene, 'settings': settings, 'population': population, 'observations': old_scene.get('observations', []),
                         'note': '任务通用场景设定与缺省值，非实测；保存参数后供当前任务的实验使用。',
                         'population_note': '当前任务的人群画像假设；没有观测依据时，不据此推算空间人数。'}
    portrait['demand'] = domain_demand.project(task, portrait)
    portrait['activities'] = domain_relations.activity_records(task, portrait, news)
    portrait['model_inputs'] = domain_relations.model_inputs(task, portrait, roads)
    portrait['occupancy'] = domain_relations.occupancy_estimate(task, portrait['demand'], settings)
    return portrait


class DomainPortraitService:
    def __init__(self, database):
        self.tasks = AnalysisTaskRepository(database)
        self.news = database['news_articles']

    def activity_news(self, task):
        from datetime import datetime, timedelta
        from ..prediction.inputs import task_times
        from ..schemas.environments import geometry_bounds
        w, s, e, n = geometry_bounds(task['region']['geometry'])
        if not (121.45 < (w+e)/2 < 121.52 and 31.21 < (s+n)/2 < 31.28):
            return []
        try:
            start, _, _ = task_times(task)
        except (ValueError, TypeError, KeyError):
            return []
        as_of = min(start.date(), datetime.now().date()).isoformat()
        return list(self.news.find({'$and': [
            {'$or': [{'title': {'$regex': '纪念塔|外滩|南京东路'}}, {'summary': {'$regex': '人民英雄纪念塔'}}]},
            {'$or': [{'title': {'$regex': '活动|跨年|迎新|健步|音乐|灯会'}}, {'summary': {'$regex': '健步走|活动举办|活动时间'}}]},
            {'publishedAt': {'$gte': (start-timedelta(days=400)).date().isoformat(), '$lte': as_of + 'T23:59:59'}}
        ]}, {'title':1,'source':1,'url':1,'publishedAt':1,'summary':1}).sort('publishedAt',-1).limit(12))

    def read(self, identifier):
        task = self.tasks.get(identifier)
        if task is None:
            raise KeyError(identifier)
        return task

    def save(self, identifier, payload, roads=None, metadata=None):
        current = self.read(identifier)
        if current['revision'] != payload['revision']:
            raise DatasetConflictError('任务已更新，请重新读取领域模型后再保存')
        portrait = build_portrait(current, roads, metadata)
        data = {key: copy.deepcopy(current[key]) for key in TASK_FIELDS}
        section = payload['section']
        value = copy.deepcopy(payload['data'])
        if section == 'demand_initialize':
            if value != {} or portrait.get('demand_inputs'):
                raise ValueError('已有多源资料请直接编辑，不会重新覆盖为示例')
            portrait['demand_inputs'] = domain_demand.initialize(current, portrait)
            domain_demand.estimate(current, portrait['demand_inputs'])
        elif section == 'demand':
            domain_demand.estimate(current, value)
            portrait['demand_inputs'] = domain_demand.validate(current, value)
        elif section == 'demand_source':
            if not isinstance(value, dict) or set(value) != {'source', 'dataset'} or value['source'] not in ('history', 'transport', 'weather') or not portrait.get('demand_inputs'):
                raise ValueError('请提供 history、transport 或 weather 数据源及完整 dataset；须先建立多源估计')
            config = {**portrait['demand_inputs'], value['source']: value['dataset']}
            domain_demand.estimate(current, config)
            portrait['demand_inputs'] = domain_demand.validate(current, config)
        elif section == 'demand_apply':
            demand = domain_demand.project(current, portrait)
            if demand['status'] != 'ready' or value != {'fingerprint': demand['fingerprint']}:
                raise ValueError('估计已经变化或资料未对齐，请重新读取后再使用')
            data['components']['forecast_setup'] = normalize_settings(current, demand['forecast_inputs'])
            portrait['demand_application'] = {'fingerprint': demand['fingerprint'], 'task_revision': current['revision'] + 1,
                                             'method': demand['method'], 'inputs': copy.deepcopy(portrait['demand_inputs']),
                                             'outputs': demand['forecast_inputs'], 'total_visits': demand['total_visits']}
        elif section == 'evidence':
            if not isinstance(value, dict) or set(value) != {'evidence', 'candidates'}:
                raise ValueError('请提供资料和候选线索')
            for key in value:
                rows = value[key]
                if not isinstance(rows, list) or len(rows) > 500 or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) or not isinstance(row.get('title'), str) for row in rows):
                    raise ValueError('资料和线索须包含有效标识与标题，最多 500 条')
                if len({row['id'] for row in rows}) != len(rows):
                    raise ValueError('资料和线索标识不能重复')
            portrait.update(value)
        elif section == 'settings':
            if not isinstance(value, dict) or set(value) != set(EDITABLE_SETTINGS):
                raise ValueError('请提供完整的通用场景参数')
            data['time_window'] = payload.get('time_window', current['time_window'])
            # Include the preview's common scene once when explicitly saving it.
            settings = normalize_settings(data, {**portrait['scene']['settings'], **value})
            data['components']['forecast_setup'] = settings
            portrait['scene']['settings'] = {key: settings[key] for key in COMMON_FIELDS}
        elif section == 'population':
            if not isinstance(value, dict):
                raise ValueError('人群画像须为对象')
            data['population'] = value
            portrait['scene']['population'] = value
        elif section == 'observations':
            if not isinstance(value, list) or len(value) > 1000:
                raise ValueError('空间人数记录最多 1000 条')
            from datetime import datetime
            for row in value:
                if not isinstance(row, dict) or type(row.get('count')) is not int or not 0 <= row['count'] <= 1000000 or row.get('kind') not in ('假设', '实测') or not isinstance(row.get('source'), str) or not row['source'].strip():
                    raise ValueError('人数记录须有有效人数、记录性质和来源')
                coordinate(row.get('coordinates'))
                datetime.fromisoformat(row.get('at', ''))
            portrait['scene']['observations'] = value
        else:
            raise ValueError('不支持的领域模型修改')
        # An explicitly saved former preview carries its accepted scene forward.
        # Reading the model never changes canonical inputs or task revisions.
        if current['components'].get('domain_portrait', {}).get('mode') == 'preview':
            data['population'] = portrait['scene']['population']
            if section != 'settings':
                data['components']['forecast_setup'] = normalize_settings(data, portrait['scene']['settings'])
        for derived in ('activities', 'model_inputs', 'occupancy'):
            portrait.pop(derived, None)
        portrait.pop('demand', None)  # Derived on read; never persist a stale result.
        data['components']['domain_portrait'] = portrait
        return self.tasks.save(data, identifier, current['revision'])
