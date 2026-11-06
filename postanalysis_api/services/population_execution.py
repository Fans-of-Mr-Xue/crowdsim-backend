"""Compile observed population targets into the current post-review society."""
from copy import deepcopy
import math

from postanalysis_api.schemas.common import ApiError

MODE = 'population-targets/1.0'


def prepare_execution_dataset(dataset):
    metadata = dataset.get('metadata') or {}
    profile = metadata.get('executionProfile')
    if metadata.get('sourceRecords') or profile is not None and (not isinstance(profile, dict) or profile.get('inputMode') != MODE):
        return dataset
    rows = dataset.get('observations') or []
    if len(rows) < 2 or any(row.get('regionId') != 'global' for row in rows):
        return dataset
    duration = math.ceil(max(row['timeSeconds'] for row in rows))
    if not 2 <= duration <= 86400:
        return dataset
    controls = {'gateOpenSeconds': max(1, int(duration * .6)), 'guidanceStartSeconds': max(1, int(duration * .6)),
                'guidanceDelaySeconds': max(0, int(duration * .05)), 'compliance': .4}
    from postanalysis_api.workers.memorial_scenario import ENTRY_EDGES
    result = {**dataset, 'metadata': {**metadata, 'durationSeconds': duration,
        'executionProfile': {'scenarioId': 'memorial-post-test', 'inputMode': MODE, 'baselineControl': controls,
                             'regionMapping': {key: {'edgeId': edge} for key, edge in ENTRY_EDGES.items()}},
        'reconstruction': {'version': MODE, 'sourceDatasetVersion': dataset.get('version'),
                           'target': 'global_population', 'initialObservationSeconds': rows[0]['timeSeconds'],
                           'initialPopulation': rows[0]['population'],
                           'initializationRule': 'current_post_society_AB_equal_entries_common_seed',
                           'controlSource': 'post_design_defaults'}}}
    population_cohorts(result)  # Validate resource bounds before exposing execution readiness.
    return result


def population_cohorts(dataset):
    rows = sorted(dataset['observations'], key=lambda row: row['timeSeconds'])
    duration = dataset['metadata']['durationSeconds']
    cohorts, active, previous = [], [], 0
    for index, row in enumerate(rows):
        count, second = row['population'], row['timeSeconds']
        if type(count) is not int or count < 0 or not math.isfinite(second) or not 0 <= second <= duration:
            raise ApiError('EXECUTION_INPUT_INVALID', '人数或观测时刻无效', 422)
        for _ in range(max(0, count - previous)):
            if len(cohorts) >= 20000:
                raise ApiError('RESOURCE_LIMIT_EXCEEDED', '单次事后执行最多支持两万人', 422)
            person = {'id': f'p_rebuilt_{len(cohorts):06d}', 'regionId': 'A' if len(cohorts) % 2 == 0 else 'B',
                      'depart': 0 if index == 0 else second, 'release': float(duration + 1),
                      'agePercent': {}, 'nationalityPercent': {}}
            cohorts.append(person); active.append(person)
        for _ in range(max(0, previous - count)):
            active.pop(0)['release'] = second
        previous = count
    return deepcopy(cohorts)


def execution_context(dataset):
    prepared = prepare_execution_dataset(dataset)
    metadata = prepared.get('metadata') or {}
    profile = metadata.get('executionProfile') or {}
    controls = profile.get('baselineControl')
    if not controls:
        return None
    return {'inputMode': profile.get('inputMode', 'source_records'), 'durationSeconds': metadata['durationSeconds'],
            'baselineExecution': {'startSeconds': controls['gateOpenSeconds'], 'compliance': controls['compliance'],
                                  'delaySeconds': controls['guidanceDelaySeconds']}}
