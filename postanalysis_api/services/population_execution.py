"""Resolve explicit spatial initialization; global totals cannot locate people."""
from postanalysis_api.schemas.common import ApiError
from .factual_context import MODE, is_spatial, spatial_cohorts


def prepare_execution_dataset(dataset):
    metadata = dataset.get('metadata') or {}
    if is_spatial(dataset):
        from .distribution_targets import distribution_targets
        targets = distribution_targets(dataset)
        if not targets['inputReady']:
            raise ApiError('FACTUAL_RECONSTRUCTION_REQUIRED', '；'.join(targets['missing']), 422)
        spatial_cohorts(dataset)
        return dataset
    if metadata.get('sourceBinding', {}).get('sourceKind') == 'mongodb_archive':
        raise ApiError('FACTUAL_REIMPORT_REQUIRED', '此旧数据集只保留全局摘要，请重新从事中归档导入完整空间目标。', 422)
    if (metadata.get('executionProfile') or {}).get('inputMode') == 'population-targets/1.0':
        raise ApiError('FACTUAL_RECONSTRUCTION_REQUIRED', '旧的全局人数初始化缺少空间分布，请重新导入空间目标。', 422)
    return dataset


def population_cohorts(dataset):
    if not is_spatial(dataset):
        raise ApiError('FACTUAL_RECONSTRUCTION_REQUIRED', '初始人群需要空间分布，不能按固定比例分配入口。', 422)
    return spatial_cohorts(dataset)


def execution_context(dataset):
    prepared = prepare_execution_dataset(dataset)
    metadata = prepared.get('metadata') or {}
    profile = metadata.get('executionProfile') or {}
    controls = profile.get('baselineControl')
    if not controls:
        return None
    return {'inputMode': profile.get('inputMode', 'source_records'), 'durationSeconds': metadata['durationSeconds'],
            'baselineExecution': {'startSeconds': controls['guidanceStartSeconds'], 'compliance': controls['compliance'],
                                  'delaySeconds': controls['guidanceDelaySeconds']},
            'controlSource': metadata.get('reconstruction', {}).get('controlSource')}
