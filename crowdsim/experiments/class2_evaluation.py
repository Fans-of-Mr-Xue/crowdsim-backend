"""Read archived class-two evidence without connecting to or advancing an engine."""
from copy import deepcopy
import hashlib
import json
import math
from statistics import mean, stdev

from .during_observation import METRICS


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def statistics(values):
    values = [v for v in values if finite(v)]
    return {'n': len(values), 'mean': mean(values) if values else None,
            'std': stdev(values) if len(values) > 1 else None,
            'min': min(values) if values else None, 'max': max(values) if values else None}


def topsis(rows, definitions):
    """Equal-weight vector-normalized TOPSIS; constants and missing columns do not rank."""
    available = [d for d in definitions if all(finite(r.get(d['key'])) for r in rows)]
    columns = [d for d in available if len({r[d['key']] for r in rows}) > 1]
    if len(rows) < 2:
        return {'scores': [None] * len(rows), 'criteria': [], 'reason': '至少需要两个具有共同有效轮次的决策'}
    if not available:
        return {'scores': [None] * len(rows), 'criteria': [], 'reason': '缺少共同有效评分指标'}
    if not columns:
        return {'scores': [50.0] * len(rows), 'criteria': [], 'reason': '有效指标无差异，决策并列'}
    matrix = [[r[d['key']] / math.sqrt(sum(x[d['key']] ** 2 for x in rows)) / len(columns)
               for d in columns] for r in rows]
    best = [(max if d['higherIsBetter'] else min)(r[j] for r in matrix) for j, d in enumerate(columns)]
    worst = [(min if d['higherIsBetter'] else max)(r[j] for r in matrix) for j, d in enumerate(columns)]
    scores = []
    for row in matrix:
        positive = math.sqrt(sum((v-b) ** 2 for v, b in zip(row, best)))
        negative = math.sqrt(sum((v-b) ** 2 for v, b in zip(row, worst)))
        scores.append(100 * negative / (positive+negative) if positive+negative else 50.0)
    return {'scores': scores, 'criteria': [{**d, 'weight': 1/len(columns)} for d in columns],
            'reason': '共同种子、相同观察窗口；非恒定有效指标等权；分数仅表示本组内的相对表现'}


def _read_samples(root, record, row):
    if not row.get('sampleArchive'):
        return [], '缺少逐时刻采样档案'
    path = (root / row['sampleArchive']).resolve()
    expected = (root / record['conversationId'] / row['id'] / f"attempt-{row['attempt']}.samples.jsonl").resolve()
    if path != expected or not path.is_relative_to(root.resolve()):
        raise ValueError('采样档案路径与本轮来源不一致')
    try:
        samples = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        times = [s['timeSeconds'] for s in samples]
        start, end = record['warmupSeconds'], record['executionRequest']['durationSeconds']
        if (not times or len(times) != row.get('sampleCount') or any(not finite(t) for t in times)
                or times[0] <= start or abs(times[-1]-end) > 1e-6
                or any(a >= b for a, b in zip(times, times[1:]))):
            return [], '采样数量、时间顺序或观察窗口不完整'
        return samples, ''
    except (OSError, ValueError, KeyError, TypeError):
        return [], '采样档案缺失或格式错误'


def _measure(samples, definitions, start):
    result = {}
    for d in definitions:
        values = [s.get(d['key']) for s in samples]
        if values and all(finite(v) for v in values):
            result[d['key']] = mean(values)
    if not samples or not all(isinstance(s.get('flowState'), dict) for s in samples):
        return result
    states = [s['flowState'] for s in samples]
    final = states[-1]
    if finite(final.get('total')) and final['total'] > 0 and finite(final.get('arrived')):
        result['arrival-ratio'] = final['arrived'] / final['total'] * 100
    for field, key in [('plannedWaiting', 'planned-wait'), ('slowWaiting', 'slow-wait')]:
        if all(finite(s.get(field)) for s in states):
            times = [start, *[s['timeSeconds'] for s in samples]]
            result[key] = sum((times[i+1]-times[i])*s[field] for i, s in enumerate(states))
    speeds = [s.get('meanTravelSpeedMps') for s in states]
    valid_speeds = [s for s in speeds if finite(s)]
    if valid_speeds:
        result['travel-speed'] = mean(valid_speeds)
    return result


EXTRA_METRICS = [
    {'key': 'arrival-ratio', 'label': '观察结束时到达终点比例', 'unit': '%', 'higherIsBetter': True},
    {'key': 'planned-wait', 'label': '管理暂缓累计人秒', 'unit': '人·秒', 'higherIsBetter': False},
    {'key': 'slow-wait', 'label': '非计划持续低速累计人秒', 'unit': '人·秒', 'higherIsBetter': False},
    {'key': 'travel-speed', 'label': '行进人群平均速度', 'unit': '米/秒', 'higherIsBetter': True},
]


def build_evaluation(record, root, requirement):
    definitions = [{'key': k, 'label': METRICS[k][0], 'unit': METRICS[k][1],
                    'higherIsBetter': k in {'global-speed', 'local-speed'}}
                   for k in record['metricIds'] if k in METRICS]
    selected_definitions = definitions[:]
    definitions += EXTRA_METRICS
    runs, evidence, series = [], [], {}
    for row in record['runs']:
        samples, error = _read_samples(root, record, row) if row['status'] == 'completed' else ([], '本轮尚未正常完成')
        metrics = _measure(samples, selected_definitions, record['warmupSeconds'])
        series[row['id']] = samples
        # Hash full-resolution evidence, not the downsampled display curves.
        evidence.append({'row': row, 'samples': samples})
        stride = max(1, math.ceil(len(samples)/240))
        display = samples[::stride]
        if samples and display[-1] is not samples[-1]:
            display.append(samples[-1])
        events, previous = [], {}
        for sample in samples:
            for receipt in sample.get('actionUpdates', []):
                identity = receipt.get('actionId')
                status = receipt.get('status')
                if previous.get(identity) != status:
                    actual = receipt.get('statusChangedAtSeconds')
                    exact = finite(actual) and record['warmupSeconds'] <= actual <= sample['timeSeconds']
                    events.append({'timeSeconds': actual if exact else sample['timeSeconds'],
                                   'observedAtSeconds': sample['timeSeconds'], 'timeSource': 'receipt' if exact else 'sample', 'actionId': identity,
                                   'status': status, 'receipt': receipt})
                    previous[identity] = status
        extrema = []
        for key, operation, title in [('local-density', max, '局部最大密度峰值'), ('global-speed', min, '全局平均速度最低点')]:
            points = [s for s in samples if finite(s.get(key))]
            if points:
                point = operation(points, key=lambda p: p[key])
                extrema.append({'timeSeconds': point['timeSeconds'], 'title': title, 'metric': key, 'value': point[key]})
        windows, seen = [], set()
        for event in events:
            if not event['actionId'] or event['status'] in {'scheduled', 'cancelled'} or event['actionId'] in seen:
                continue
            seen.add(event['actionId'])
            t = event['timeSeconds']
            before = [s for s in samples if max(record['warmupSeconds'], t-10) <= s['timeSeconds'] < t]
            after = [s for s in samples if t <= s['timeSeconds'] <= t+10]
            windows.append({'actionId': event['actionId'], 'eventTimeSeconds': t, 'timeSource': event['timeSource'],
                            'observedAtSeconds': event['observedAtSeconds'],
                            'beforeWindow': [max(record['warmupSeconds'], t-10), t],
                            'afterWindow': [t, min(record['executionRequest']['durationSeconds'], t+10)],
                            'metrics': {d['key']: {'before': statistics(s.get(d['key']) for s in before),
                                                  'after': statistics(s.get(d['key']) for s in after)} for d in selected_definitions}})
        runs.append({k: deepcopy(row.get(k)) for k in ('id', 'candidateId', 'round', 'seed', 'status', 'attempt', 'runtimeRunId')})
        runs[-1].update(valid=bool(samples), error=error or row.get('error', ''), metrics=metrics,
                        metricRanges={d['key']: statistics(s.get(d['key']) for s in samples) for d in selected_definitions},
                        series=[{k: s[k] for k in ('timeSeconds', *record['metricIds']) if k in s} for s in display],
                        extrema=extrema, actionEvents=events, actionWindows=windows,
                        flowState=samples[-1].get('flowState') if samples else None,
                        actionReceipts=row.get('actionReceipts', []), sampleCount=len(samples))
    by_candidate = {c['id']: {r['seed']: r for r in runs if r['candidateId'] == c['id'] and r['valid']} for c in record['candidates']}
    common = sorted(set.intersection(*(set(rows) for rows in by_candidate.values()))) if by_candidate else []
    # Missing/unaligned samples must not give one decision a shorter observation window.
    common = [seed for seed in common if len({tuple(s['timeSeconds'] for s in series[rows[seed]['id']])
                                             for rows in by_candidate.values()}) == 1]
    candidates = []
    for candidate in record['candidates']:
        rows = list(by_candidate[candidate['id']].values())
        paired = [by_candidate[candidate['id']][seed] for seed in common]
        candidates.append({**deepcopy(candidate), 'completedRuns': len(rows), 'requestedRuns': record['repetitions'],
                           'metrics': {d['key']: statistics(r['metrics'].get(d['key']) for r in rows) for d in definitions},
                           'pairedMetrics': {d['key']: statistics(r['metrics'].get(d['key']) for r in paired) for d in definitions}})
    matrix = [{d['key']: c['pairedMetrics'][d['key']]['mean']
               if c['pairedMetrics'][d['key']]['n'] == len(common) and common else None for d in definitions} for c in candidates]
    # Global/local speed include destination holds: retain them as observations, not independent efficiency scores.
    criteria = [d for d in definitions if d['key'] not in {'global-speed', 'local-speed'}]
    ranking = topsis(matrix, criteria) if common else {'scores': [None]*len(candidates), 'criteria': [], 'reason': '没有共同完成且采样时刻一致的种子'}
    for candidate, score in zip(candidates, ranking.pop('scores')):
        candidate['score'] = score
        candidate['rank'] = None
    # Compute ranks after all scores have been assigned, preserving ties.
    for c in candidates:
        if c['score'] is not None:
            c['rank'] = 1 + sum(other['score'] > c['score'] + 1e-8 for other in candidates)
    pairwise = []
    for i, left in enumerate(candidates):
        for right in candidates[i+1:]:
            deltas = {}
            for d in definitions:
                values = [(by_candidate[left['id']][seed]['metrics'].get(d['key']), by_candidate[right['id']][seed]['metrics'].get(d['key'])) for seed in common]
                deltas[d['key']] = statistics(a-b for a, b in values if finite(a) and finite(b))
            pairwise.append({'left': left['id'], 'right': right['id'], 'metrics': deltas})
    source = {'conversationId': record['conversationId'], 'snapshotId': record['snapshotId'],
              'experimentId': record['experimentId'], 'executionVersion': record['executionVersion'],
              'generationId': record['generationId'], 'generationRevision': record['generationRevision']}
    source['fingerprint'] = fingerprint({'evaluationVersion': 1, 'source': source, 'candidates': record['candidates'], 'evidence': evidence,
                                         'requirement': requirement, 'warmup': record['warmupSeconds']})
    baseline_candidate = next((c for c in candidates if c['actions'] and all(a['actionType'] == 'observe_only' for a in c['actions'])), None)
    baseline = {'available': False, 'reason': '本组未配置独立无干预对照；只进行决策之间的比较'}
    if baseline_candidate and common:
        baseline = {'available': True, 'candidateId': baseline_candidate['id'],
                    'reason': f"以本组仅记录观测的“{baseline_candidate['label']}”作为同配置、同种子无干预对照", 'comparisons': []}
        for candidate in candidates:
            if candidate['id'] == baseline_candidate['id']:
                continue
            improvements = {}
            for d in definitions:
                pairs = [(by_candidate[baseline_candidate['id']][seed]['metrics'].get(d['key']),
                          by_candidate[candidate['id']][seed]['metrics'].get(d['key'])) for seed in common]
                improvements[d['key']] = statistics((v-b)/abs(b)*100*(1 if d['higherIsBetter'] else -1)
                                                     for b, v in pairs if finite(b) and finite(v) and b != 0)
            baseline['comparisons'].append({'candidateId': candidate['id'], 'improvementPercent': improvements})
    from crowdsim.scenarios.class2_strategy_tool import strategy_tool, ACTIONS
    return {'schemaVersion': 1, **source, 'status': record['status'], 'title': requirement['project']['title'],
            'requirement': requirement, 'toolEffects': strategy_tool(ACTIONS, {})['effects'],
            'window': {'startSeconds': record['warmupSeconds'], 'endSeconds': record['executionRequest']['durationSeconds']},
            'population': requirement['population']['total'], 'flows': requirement['class2']['flow_design']['flows'],
            'definitions': definitions, 'candidates': candidates, 'runs': runs, 'commonSeeds': common,
            'pairwise': pairwise, 'ranking': ranking, 'baseline': baseline,
            'dataGaps': (["部分轮次缺少到达、管理暂缓及持续低速采样，相关指标不参与缺失轮次的评分"]
                         if any(r['valid'] and not r['flowState'] for r in runs) else [])}

