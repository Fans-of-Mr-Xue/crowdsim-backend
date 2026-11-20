"""Production dispatcher: reuse local numerical cores and adapt source analyses."""
from collections import defaultdict
import gzip
import hashlib
import json
from statistics import mean

from bson import ObjectId

from algorithms.metrics import compare_metrics
from algorithms.numeric import pearson
from algorithms.mechanism_graph import compute_key_nodes, list_lag_effects, format_loops
from algorithms.powergame.trajectory_statistics import TrajectoryAnalysisService
from algorithms.powergame.outcome_forecast import estimate_world_outcome_forecasts, analyse_forecast_key_points
from algorithms.powergame.analysis import analyze, _contrasts
from algorithms.powergame.contract import normalize
from algorithms.powergame.dynamic_catalog import build_catalog, discover_actions
from algorithms.powergame.extraction import GuardedLLMExtractor, ExtractionError
from .analysis_inputs import trajectory_groups, horizontal_input, crowd_design, finite
from .analysis_model import AnalysisModel, ForecastClient, AnalysisCancelled

VERSION = 'crowd-source-analysis/1.1'
SOURCE = {'backend': '25489c09f2a6a4d87f5ea14c311a41f204e28ebf', 'frontend': 'db18e9131fdd0fd9ae4282eb98eb0517e98d423e'}
# The owner selected the source's first-definitive-outcome target. Crowd
# terminal conditions still need definition; do not substitute window-end states.
FORECAST_OUTCOMES = ()


def _metric_comparison(baseline, candidate):
    def values(group):
        buckets = defaultdict(list)
        for world in group['trajectories']:
            if world.get('error'):
                continue
            last = world.get('ticks', [])[-1:] or [{}]
            for prefix, row in (('world_state.', last[0].get('world_state', {})), ('rv.', world.get('rv_values', {}))):
                for key, value in row.items():
                    if finite(value):
                        buckets[prefix + key].append(value)
        return {key: mean(rows) for key, rows in buckets.items()}
    left, right = values(baseline), values(candidate)
    # This is exactly the source OAT delta convention, through the existing core.
    rows = compare_metrics(left, right, [{'key': key, 'direction': 'up'} for key in sorted(left.keys() | right.keys())])
    return {'absolute_deltas': {row['key']: round(row['change'], 4) for row in rows if row['available']},
            'relative_deltas': {row['key']: round(row['improvementPct'] / 100, 4) for row in rows if row['available'] and row['improvementPct'] is not None},
            'algorithm': 'algorithms.metrics.compare_metrics', 'relativeBaselineZero': 'undefined; omitted'}


def _micro_macro(groups):
    """Descriptive associations only. No force/threshold is inferred from r."""
    micro = ['agent_moving_samples', 'agent_stopped_samples', 'agent_accelerating_samples',
             'agent_decelerating_samples', 'trusted_messages', 'applied_decisions']
    macro = ['density', 'local_peak_density', 'speed', 'pressure_proxy']
    graphs = []
    for group in groups:
        for world in group['trajectories']:
            if world.get('error') or world.get('behaviorEvidenceStatus') != 'verified':
                continue
            graph = {'microNodes': [{'id': key, 'name': key} for key in micro],
                     'macroNodes': [{'id': key, 'name': key} for key in macro],
                     'microEdges': [], 'aggregationEdges': [], 'feedbackEdges': [], 'macroEdges': [], 'loops': []}
            for source in micro:
                for target in macro:
                    pairs = [(tick['world_state'].get(source), tick['world_state'].get(target)) for tick in world['ticks']]
                    pairs = [(a, b) for a, b in pairs if finite(a) and finite(b)]
                    if len(pairs) < 3:
                        continue
                    r = pearson([a for a, _ in pairs], [b for _, b in pairs])
                    if finite(r):
                        graph['aggregationEdges'].append({'source': source, 'target': target, 'effect': r,
                            'weight': 1, 'lag': 0, 'kind': 'aggregation', 'method': 'within_run_Pearson',
                            'sampleCount': len(pairs), 'verified': False, 'causalEffect': False})
            graphs.append({'runId': world['world_id'], 'seed': world['seed'], 'groupId': group['group_id'],
                'status': 'descriptive_only' if graph['aggregationEdges'] else 'insufficient_variation',
                'graph': graph, 'keyNodes': compute_key_nodes(graph), 'lagEffects': list_lag_effects(graph), 'loops': format_loops(graph)})
    return {'status': 'descriptive_only' if graphs else 'missing_behavior_evidence', 'worlds': graphs,
            'effectMeaning': 'within-run window correlation; not a physical force or causal coefficient',
            'independentSampleCount': len({row['seed'] for row in graphs}),
            'sampleCountByGroup': {group['group_id']: len({row['seed'] for row in graphs if row['groupId'] == group['group_id']}) for group in groups},
            'seedPairing': 'same seed across strategies is a paired world, not another independent seed',
            'windowSamplesAreIndependent': False,
            'roundGateStatus': 'requires_explicit_model_driver_weights_and_thresholds',
            'limitations': ['同一运行的时间窗相关不能证明微观行为造成宏观拥堵。',
                           '相关系数只供既有图摘要计算，不进入do传播或原机制阈值门控。',
                           '没有实际响应事件证据时不估计引导响应滞后，也不预编反馈回路。']}


def _contrasts_for_references(task, groups, catalog, canonical_data=None):
    by_id = {group['group_id']: group for group in groups}
    canonical_data = canonical_data or normalize(horizontal_input(task, groups))
    canonical_worlds = {world['trajectory']['world_id']: world for world in canonical_data['worlds']}
    contrasts = []
    for group in task.get('groups', []):
        reference = group.get('referenceGroupId', 'F-00')
        pair = [by_id[reference], by_id[group['id']]]
        payload = horizontal_input(task, pair)
        records = {}
        pair_data = normalize(payload)
        # Pair normalization renumbers g/w addresses. Keep the canonical full
        # batch addresses so every citation resolves through worldBindings.
        pair_data['worlds'] = [canonical_worlds[world['trajectory']['world_id']] for world in pair_data['worlds']]
        rows = _contrasts(pair_data, records, catalog)
        contrasts.append({'groupId': group['id'], 'referenceGroupId': reference, 'edges': rows,
                          'evidence_records': records, 'comparisonUnit': 'declared strategy; actual reference preserved'})
    return contrasts


def _forecast_timelines(forecasts, groups):
    """Output binding only: mean valid model distributions at actual windows."""
    result = []
    for group in groups:
        buckets = defaultdict(list)
        times = defaultdict(list)
        for world in group['trajectories']:
            clock = {tick['tick_idx']: tick['timeSeconds'] for tick in world['ticks']}
            for point in forecasts['worlds'].get(world['world_id'], {}).get('points', []):
                if point['probabilities'] is not None:
                    buckets[point['tick_idx']].append(point['probabilities'])
                    times[point['tick_idx']].append(clock[point['tick_idx']])
        result.append({'groupId': group['group_id'], 'points': [
            {'tick_idx': tick, 'timeSeconds': max(times[tick]), 'earliestEvidenceSeconds': min(times[tick]),
             'validWorldCount': len(rows),
             'totalWorldCount': len(group['trajectories']),
             'probabilities': {category['key']: mean(row[category['key']] for row in rows)
                               for category in forecasts['categories']}}
            for tick, rows in sorted(buckets.items())]})
    return result


def complete_analysis(repository, task, runs, result, *, cancelled=lambda: False, progress=None, caller=None, outcome_categories=None):
    """Called by the existing owned batch worker after SUMO, before result sealing."""
    def checkpoint():
        if cancelled():
            raise AnalysisCancelled()
    def update(phase, detail=None):
        checkpoint()
        if progress:
            progress(phase, detail or {})
    try:
        update('reading_evidence')
        groups = trajectory_groups(repository, task, runs, checkpoint)
        by_id = {group['group_id']: group for group in groups}
        facts = [{'groupId': group['group_id'], **TrajectoryAnalysisService.analyze_group(group)} for group in groups]
        comparisons = [{'groupId': group['id'], 'referenceGroupId': group.get('referenceGroupId', 'F-00'),
                        **_metric_comparison(by_id[group.get('referenceGroupId', 'F-00')], by_id[group['id']])}
                       for group in task.get('groups', [])]
        micro_macro = _micro_macro(groups)
        model = caller or AnalysisModel(checkpoint)
        def bounded_call(prompt, *, timeout=60):
            checkpoint()
            update('model_request', {'completedCalls': getattr(model, 'calls', None)})
            response = model(prompt, timeout=timeout)
            checkpoint()
            return response
        update('forecasting')
        categories = FORECAST_OUTCOMES if outcome_categories is None else outcome_categories
        if categories:
            forecasts = estimate_world_outcome_forecasts(groups, categories,
                '依据当前人群仿真及原生动作，预测从起始事件起首先达到的明确人群终局；不推断历史事故必然性。',
                lambda: ForecastClient(bounded_call))
            analyse_forecast_key_points(forecasts, groups, '同一人群首次明确终局与完整轨迹摘要', lambda: ForecastClient(bounded_call))
        else:
            forecasts = {'categories': [], 'worlds': {}, 'reason': '已选择首次明确终局；人群终局条件尚待确定，未使用窗口末四态代替。'}
        valid = sum(point['probabilities'] is not None for world in forecasts['worlds'].values() for point in world['points'])
        total = sum(len(world['points']) for world in forecasts['worlds'].values())
        keypoint_errors = sum('key_points_error' in world for world in forecasts['worlds'].values())
        forecasts['status'] = ('terminal_definition_required' if not categories else
            'computed' if total and valid == total and not keypoint_errors else 'partial' if valid else 'model_unavailable_or_invalid')
        forecasts['target'] = 'first_definitive_outcome'
        forecasts['validPointCount'], forecasts['totalPointCount'] = valid, total
        forecasts['keyPointErrorCount'] = keypoint_errors
        forecasts['groupTimelines'] = _forecast_timelines(forecasts, groups)
        update('extracting_paths')
        payload = horizontal_input(task, groups)
        data = normalize(payload)
        conditional = []
        try:
            catalog = build_catalog(data, crowd_design(), [])
            conditional = _contrasts_for_references(task, groups, catalog, data)
            actions, attempts = discover_actions(data, bounded_call)
            catalog = build_catalog(data, crowd_design(), actions)
            extractor = GuardedLLMExtractor(bounded_call, nodes=catalog['nodes'], synthetic=False)
            horizontal = analyze(payload, extractor, catalog=catalog, progress=lambda state: update('extracting_paths', state))
            horizontal['actionCatalogAttempts'] = attempts
        except ExtractionError as exc:
            horizontal = {'status': 'model_unavailable_or_invalid', 'success': False, 'nodes': [], 'edges': [], 'paths': [],
                          'reason': str(exc), 'attempts': exc.attempts, 'method': {'fallback_used': False, 'causal_validation': False}}
        horizontal['limitations'] = ['候选关系为带真实引用的模型标注；提及率不是因果置信度。',
                                     '条件等权均值差使用各人群指标原单位；主效应仍使用已归档共同种子的配对统计。']
        horizontal['referenceContrasts'] = conditional
        full = {'version': VERSION, 'sourceCommits': SOURCE,
            'inputSha256': data['input_sha256'],
            'worldBindings': {world['world_ref']: {'runId': world['trajectory']['world_id'],
                'seed': world['trajectory']['seed'], 'groupId': groups[world['group_index']]['group_id'],
                'behaviorEvidenceStatus': world['trajectory']['behaviorEvidenceStatus']} for world in data['worlds']},
            'inputContract': {'worldUnit': 'one run per strategy/seed; independent across distinct seeds, paired across strategies', 'windowLimit': 8,
                              'temporalAggregation': 'all individual records counted; last real macro observation per interval',
                              'bodyEvidence': 'verified GridFS source, never UI controls or geopolitical presets'},
            'observation': {'groupSummaries': facts, 'comparisons': comparisons, 'forecasts': forecasts},
            'intervention': horizontal, 'mechanism': micro_macro}
        update('archiving_analysis')
        encoded = gzip.compress(json.dumps(full, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8'))
        file_id = repository.files.upload_from_stream('source_analysis.json.gz', encoded, metadata={
            'ownerId': task['ownerId'], 'workspaceId': task['workspaceId'], 'experimentId': task['id'],
            'datasetVersion': task['datasetVersion'], 'kind': 'post_source_analysis', 'version': VERSION})
        artifact = {'name': 'source_analysis.json.gz', 'fileId': str(file_id), 'sizeBytes': len(encoded),
                    'sha256': hashlib.sha256(encoded).hexdigest(), 'compression': 'gzip'}
        evidence_quality = {}
        for group in groups:
            for world in group['trajectories']:
                status = world['behaviorEvidenceStatus']
                evidence_quality[status] = evidence_quality.get(status, 0) + 1
        partial = (forecasts['status'] != 'computed' or horizontal['status'] != 'complete'
                   or evidence_quality.get('missing', 0) or evidence_quality.get('invalid', 0))
        result['analysisPipeline'] = {'version': VERSION, 'status': 'partial' if partial else 'computed',
            'sourceCommits': SOURCE, 'inputSha256': data['input_sha256'], 'artifacts': [artifact],
            'behaviorEvidenceQuality': evidence_quality,
            'reused': ['paired_mc', 'ofat', 'mechanism_ablation', 'pc', 'metrics', 'numeric', 'mechanism_graph'],
            'added': ['trajectory_statistics', 'conditional_contrasts', 'guarded_trace_extraction', 'outcome_forecast'],
            'inputContract': full['inputContract']}
        if partial:
            result.setdefault('diagnostics', []).append(
                f"源分析部分完成：预测={forecasts['status']}，路径={horizontal['status']}，行为证据={evidence_quality}；数值对照结果已保留。")
        # Full curves remain lossless in GridFS; keep the Mongo result bounded.
        forecast_summary = {**forecasts, 'worlds': dict(list(forecasts['worlds'].items())[:16]),
                            'worldCount': len(forecasts['worlds']), 'summaryWorldLimit': 16}
        result['observation']['sourceAnalysis'] = {'groupSummaries': facts, 'comparisons': comparisons,
            'forecasts': forecast_summary, 'artifact': artifact, 'status': forecasts['status']}
        edges = [{**{key: value for key, value in edge.items() if key not in {'explanations', 'world_refs', 'evidence_ids'}},
                  'explanations': edge.get('explanations', [])[:3], 'world_refs': edge.get('world_refs', [])[:20],
                  'evidence_ids': edge.get('evidence_ids', [])[:20]} for edge in horizontal['edges'][:128]]
        result['intervention']['sourceAnalysis'] = {'status': horizontal['status'], 'nodes': horizontal['nodes'],
            'edges': edges, 'paths': horizontal['paths'], 'referenceContrasts': conditional,
            'edgeCount': len(horizontal['edges']), 'summaryLimits': {'edges': 128, 'worldRefsPerEdge': 20, 'explanationsPerEdge': 3},
            'artifact': artifact, 'evidenceLocation': 'source_analysis.json.gz:intervention.evidence_records'}
        # Large per-world graphs live in GridFS. Counts never masquerade as effects.
        result['mechanism']['sourceAnalysis'] = {'status': micro_macro['status'],
            'worldCount': len(micro_macro['worlds']), 'effectMeaning': micro_macro['effectMeaning'],
            'independentSampleCount': micro_macro['independentSampleCount'],
            'roundGateStatus': micro_macro['roundGateStatus'], 'artifact': artifact,
            'roundUnit': 'population response phase following one guidance',
            'roundBoundaryStatus': 'requires_response_end_rule',
            'pathCandidates': {'status': horizontal['status'], 'nodes': horizontal['nodes'], 'edges': edges, 'paths': horizontal['paths']}}
        return result
    except AnalysisCancelled:
        raise InterruptedError('事后分析已取消') from None


def read_artifact(repository, experiment_id, user, workspace, offset=0, limit=65536):
    import base64
    from postanalysis_api.schemas.common import ApiError
    kind = 'simulations' if experiment_id.startswith('sim_') else 'experiments'
    task = repository.get(kind, experiment_id, user, workspace)
    if not task.get('resultId') or task.get('status') not in {'completed', 'partial'}:
        raise ApiError('RESULT_NOT_READY', '分析结果尚未封存', 409)
    result = repository.get('results', task['resultId'], user, workspace)
    if result.get('datasetVersion') != task['datasetVersion']:
        raise ApiError('RESULT_VERSION_MISMATCH', '结果与实验数据版本不一致', 409)
    items = result.get('analysisPipeline', {}).get('artifacts', [])
    if not items:
        raise ApiError('ANALYSIS_UNAVAILABLE', '此历史批次没有源算法分析工件', 409)
    item = items[0]
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 262144:
        raise ApiError('INVALID_QUERY', '读取范围无效', 400)
    with repository.files.open_download_stream(ObjectId(item['fileId'])) as stream:
        metadata = stream.metadata or {}
        expected = {'ownerId': user, 'workspaceId': workspace, 'experimentId': experiment_id, 'datasetVersion': task['datasetVersion']}
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise ApiError('RESOURCE_FORBIDDEN', '分析工件绑定不一致', 403)
        if offset > stream.length:
            raise ApiError('INVALID_QUERY', '读取偏移超过文件大小', 400)
        stream.seek(offset)
        content = stream.read(limit)
    return {**item, 'offset': offset, 'nextOffset': offset + len(content), 'eof': offset + len(content) >= item['sizeBytes'],
            'encoding': 'base64', 'content': base64.b64encode(content).decode('ascii')}
