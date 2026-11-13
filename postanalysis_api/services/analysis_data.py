"""Stream archived runs into chart summaries without shipping raw batch histories."""
from collections import defaultdict
from math import isfinite

from postanalysis_api.schemas.common import ApiError

FIELDS = ('density', 'meanSpeed', 'pressureProxy', 'population')
OUTCOMES = ('stable', 'congested', 'unstable', 'recovering')
RUN_FIELDS = ('id', 'experimentId', 'datasetVersion', 'planId', 'seed', 'status', 'metrics', 'controls',
              'measurement.arrivedCount', 'executionEvidence.assignedExitCounts')


def outcome(row, previous):
    density = row.get('density')
    if not isinstance(density, (int, float)) or not isfinite(density):
        return None
    if density >= 5:
        return 'unstable'
    if density >= 3.5:
        return 'congested'
    if previous.get('density') is not None and previous['density'] >= 3.5 and density < previous['density']:
        return 'recovering'
    return 'stable'


def probabilities(counts):
    total = sum(counts.values())
    return {key: counts[key] / total * 100 if total else 0 for key in OUTCOMES}


def prepare_analysis(repo, item_id, user, workspace):
    batch = repo.get('experiments', item_id, user, workspace)
    if batch['status'] != 'completed' or not batch.get('resultId'):
        raise ApiError('RESULT_NOT_READY', '请选择已完成并封存结果的反事实实验。', 409)
    result = repo.get('results', batch['resultId'], user, workspace)
    if result.get('datasetVersion') != batch['datasetVersion']:
        raise ApiError('RESULT_VERSION_MISMATCH', '结果与批次数据版本不一致', 409)
    projection = {key: 1 for key in (*RUN_FIELDS, 'timeline')}
    projection['_id'] = 0
    query = {'experimentId': item_id, 'ownerId': user, 'workspaceId': workspace, 'status': 'completed'}
    rows, time_counts, final_counts = {}, {}, defaultdict(lambda: defaultdict(int))
    metadata = []
    # A small cursor batch bounds server memory even for old, large BSON runs.
    cursor = repo._collection('runs').find(query, projection).sort([('createdAt', 1), ('id', 1)]).batch_size(1)
    for run in cursor:
        if run.get('datasetVersion') != batch['datasetVersion']:
            raise ApiError('RUN_VERSION_MISMATCH', '运行与批次数据版本不一致', 409)
        group = run['planId']
        previous = {}
        timeline = run.pop('timeline', [])
        for row in timeline:
            key = (group, row['timeSeconds'])
            accum = rows.setdefault(key, {field: [0, 0] for field in FIELDS})
            for field in FIELDS:
                value = row.get(field)
                if isinstance(value, (int, float)) and isfinite(value):
                    accum[field][0] += value
                    accum[field][1] += 1
            state = outcome(row, previous)
            if state:
                time_counts.setdefault(key, defaultdict(int))[state] += 1
            previous = row
        state = outcome(timeline[-1], timeline[-2] if len(timeline) > 1 else {}) if timeline else None
        if state:
            final_counts[group][state] += 1
        run['timelineCount'] = len(timeline)
        metadata.append(run)
    seconds = sorted(time for group, time in rows if group == 'F-00')
    groups = ['F-00', *(group['id'] for group in batch['groups'])]
    rows_by_group = {group: [dict(timeSeconds=time, **{
        field: values[0] / values[1] if values[1] else None
        for field, values in rows.get((group, time), {field: [0, 0] for field in FIELDS}).items()})
        for time in seconds] for group in groups}
    # The original baseline rows are replaced by the computed chart series.
    batch.get('reviewSnapshot', {}).get('factualBaseline', {}).pop('rows', None)
    return {'batch': batch, 'runs': metadata,
            'sections': {name: result[key] for name, key in (
                ('observation', 'observation'), ('intervention', 'intervention'),
                ('mechanism', 'mechanism'), ('causal-graph', 'causalGraph'))},
            'prepared': {'seconds': seconds, 'rowsByGroup': rows_by_group,
                         'finalProbabilities': {group: probabilities(final_counts[group]) for group in groups},
                         'baselineProbabilities': {str(time): probabilities(time_counts.get(('F-00', time), defaultdict(int))) for time in seconds}}}


def run_series(repo, run_id, user, workspace):
    run = repo._collection('runs').find_one(
        {'id': run_id, 'ownerId': user, 'workspaceId': workspace},
        {'_id': 0, 'id': 1, 'experimentId': 1, 'datasetVersion': 1, 'planId': 1, 'seed': 1, 'status': 1, 'timeline': 1})
    if run is None:
        raise ApiError('NOT_FOUND', '运行不存在或无权访问', 404)
    if run['status'] != 'completed':
        raise ApiError('RESULT_NOT_READY', '该次运行尚未完成', 409)
    return run
