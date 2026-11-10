"""Recover a same-scene F00 input from verified observations, never case defaults."""
from collections import Counter
from copy import deepcopy
import csv
import hashlib
import json
import math
from bson.errors import InvalidId
from gridfs import NoFile

from postanalysis_api.schemas.common import ApiError

MODE = 'spatial-distribution/2.0'


def current_context():
    from crowdsim.domain.observation_config import ObservationConfig
    from crowdsim.infrastructure.observation_metrics import ObservationCollector
    from postanalysis_api.workers.memorial_scenario import ROOT, NET_FILE, network
    preset = json.loads((ROOT / 'config/during_presets/memorial_tower_v1.json').read_text(encoding='utf-8'))
    experiment = json.loads((ROOT / 'config/experiment.json').read_text(encoding='utf-8'))
    boundary = preset['requirement']['spatial_scope']['boundary']
    xy = [network().convertLonLat2XY(*point) for point in boundary['coordinates'][0]]
    collector = ObservationCollector(xy, ['global-density', 'global-speed', 'local-density', 'local-speed',
                                         'boundary-density-difference'], ObservationConfig.load(), source_boundary=boundary)
    paths = ['config/experiment.json', 'config/observation_metrics.json', 'config/population_profiles.json',
             'config/crowd_hotspots.json', 'config/during_presets/memorial_tower_v1.json']
    return collector, {'geometry': collector.geometry_metadata(), 'sumo': experiment['sumo'],
        'stepSeconds': experiment['step_length_seconds'],
        'populationProfiles': json.loads((ROOT / paths[2]).read_text(encoding='utf-8')),
        'populationDistributions': preset['requirement']['population']['distributions'],
        'hotspot': next(row for row in json.loads((ROOT / paths[3]).read_text(encoding='utf-8'))['hotspots']
                        if row['id'] == preset['hotspot_id']),
        'resourceSha256': {**{path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths},
                          'scenarios/shanghai_bund/bund.net.xml': hashlib.sha256(NET_FILE.read_bytes()).hexdigest()},
        'configurationSource': 'current_during_environment_verified_against_archived_cells',
        'historicalConfigurationArchived': False}


def import_archive(reader, binding, user):
    """Called server-side on a new import; the browser transports only the binding."""
    from shapely.geometry import Point
    report = reader.report(binding.get('runId', ''), user)
    if binding.get('sourceVersion') != report['sourceVersion']:
        raise ApiError('DURING_SOURCE_CHANGED', '事中归档版本已变化，请重新读取来源。', 409)
    document = reader.db.during_results.find_one({'run_id': binding['runId']})
    if reader.version(document) != report['sourceVersion']:
        raise ApiError('DURING_SOURCE_CHANGED', '事中归档读取期间发生变化，请重新导入。', 409)
    collector, context = current_context()
    provenance = list(report['sourceBinding']['files'])
    series = report['runs'][0]['series']
    times = [point['timeSeconds'] for point in series]
    interval = report['observationIntervalSeconds']
    if (len(times) < 2 or not isinstance(interval, (int, float)) or interval <= 0 or
            any(not math.isclose(b - a, interval, abs_tol=1e-8) for a, b in zip(times, times[1:]))):
        raise ApiError('DURING_ARCHIVE_INVALID', '来源采样间隔或时间轴不完整。', 422)
    points = {point['sourceSnapshotId']: point for point in series}
    cells = {cell['cell_id']: cell for cell in collector.cells.values()}
    rows, seen, totals = [], set(), Counter()
    initial_counts = {}
    try:
        fields = {'time_seconds', 'snapshot_id', 'cell_id', 'person_count', 'area_m2',
                  'density_person_per_m2', 'avg_speed_mps'}
        for raw in reader._csv(document, 'observation_cells', fields, provenance):
            point, cell = points.get(raw['snapshot_id']), cells.get(raw['cell_id'])
            time, count, area = float(raw['time_seconds']), float(raw['person_count']), float(raw['area_m2'])
            key = (time, raw['cell_id'])
            if (point is None or cell is None or time != point['timeSeconds'] or key in seen or
                    not count.is_integer() or count < 0 or not math.isclose(area, cell['area_m2'], rel_tol=1e-8, abs_tol=1e-6)):
                raise ValueError('原网格标识、面积、时间或人数与当前场景不一致')
            density = reader._metric(raw, 'density_person_per_m2')
            if density is not None and not math.isclose(density, count / area, rel_tol=1e-8, abs_tol=1e-8):
                raise ValueError('原网格密度与人数/面积不一致')
            seen.add(key); totals[time] += int(count)
            rows.append({'timeSeconds': time, 'regionId': raw['cell_id'], 'population': int(count),
                         'density': density, 'meanSpeed': reader._metric(raw, 'avg_speed_mps'),
                         'pressureProxy': None, 'sourceSnapshotId': raw['snapshot_id']})
            if time == times[0]:
                initial_counts[raw['cell_id']] = int(count)
        if (len(seen) != len(times) * len(cells) or
                any(totals[point['timeSeconds']] != point['personCount'] for point in series)):
            raise ValueError('原时空网格不完整或人数不守恒，不得抽样、丢格或补零')
        if sum(initial_counts.values()) > 20000:
            raise ValueError('单次事后执行最多支持两万人')
        initial, person_ids = [], set()
        fields = {'time', 'person_id', 'x', 'y', 'edge', 'snapshot_id', 'observation_cell_id'}
        has_trajectory = any(item.get('role') == 'trajectory' for item in document.get('files', []))
        if has_trajectory:
            for raw in reader._csv(document, 'trajectory', fields, provenance):
                time = float(raw['time'])
                if time > times[0]:
                    break
                if time != times[0]:
                    raise ValueError('轨迹首帧时刻与观测首帧不一致')
                x, y = float(raw['x']), float(raw['y'])
                point = Point(x, y)
                cell_id = collector._cell_for(point) if collector.scope.covers(point) else None
                if (not all(math.isfinite(v) for v in (x, y)) or cell_id != raw['observation_cell_id'] or
                        raw['snapshot_id'] != series[0]['sourceSnapshotId'] or raw['person_id'] in person_ids):
                    raise ValueError('初始轨迹位置、网格、快照或人员标识异常')
                person_ids.add(raw['person_id'])
                initial.append({'regionId': cell_id, 'edgeId': raw['edge'], 'x': x, 'y': y})
        else:
            initial = [{'regionId': key} for key, count in initial_counts.items() for _ in range(count)]
        if Counter(row['regionId'] for row in initial) != Counter({key: value for key, value in initial_counts.items() if value}):
            raise ValueError('初始轨迹分布与首帧网格人数不一致')
        if len(initial) > 20000:
            raise ValueError('单次事后执行最多支持两万人')
    except (ValueError, KeyError, TypeError, OSError, InvalidId, NoFile, csv.Error, EOFError, UnicodeError) as exc:
        raise ApiError('DURING_ARCHIVE_INVALID', f'事实重构输入校验失败：{exc}', 422) from None
    context.update(samplingSeconds=interval, startSeconds=times[0], endSeconds=times[-1],
                   initialDistribution=initial, expectedRegionIds=sorted(cells), expectedTimes=times)
    duration = math.ceil(times[-1])
    metadata = {'sourceBinding': {**report['sourceBinding'], 'files': list({item['role']: item for item in provenance}.values())},
        'displayTimeline': [{'timeSeconds': point['timeSeconds'], 'regionId': 'global', 'population': point['personCount'],
            'density': point['global-density'], 'meanSpeed': point['global-speed'], 'pressureProxy': None,
            'localDensity': point['local-density'], 'localMeanSpeed': point['local-speed'],
            'boundaryDensityDifference': point['boundary-density-difference'], 'sourceSnapshotId': point['sourceSnapshotId']}
            for point in series],
        'sourceRun': report['sourceRun'], 'durationSeconds': duration, 'factualContext': context,
        'executionProfile': {'scenarioId': 'memorial-post-test', 'inputMode': MODE,
            'baselineControl': {'gateOpenSeconds': math.ceil(times[0]), 'guidanceStartSeconds': math.ceil(times[0]),
                                'guidanceDelaySeconds': 0, 'compliance': 0}},
        'reconstruction': {'version': MODE, 'target': 'cell_population_distribution',
            'initialObservationSeconds': times[0], 'initialPopulation': len(initial),
            'initializationRule': 'first_frame_cell_quotas_and_empirical_road_positions_with_shared_seed_profiles',
            'controlSource': 'unobserved_no_additional_control_assumption', 'calibrated': False}}
    return rows, metadata


def is_spatial(dataset):
    return ((dataset.get('metadata') or {}).get('executionProfile') or {}).get('inputMode') == MODE


def spatial_cohorts(dataset):
    context = dataset['metadata']['factualContext']
    initial = context['initialDistribution']
    if len(initial) > 20000:
        raise ApiError('RESOURCE_LIMIT_EXCEEDED', '单次事后执行最多支持两万人', 422)
    return [{**deepcopy(row), 'id': f'p_f00_{index:06d}', 'depart': 0,
             'release': dataset['metadata']['durationSeconds'] + context['stepSeconds']}
            for index, row in enumerate(initial)]
