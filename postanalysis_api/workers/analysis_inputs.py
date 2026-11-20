"""Map archived crowd runs to source trajectory contracts, without rerunning SUMO."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math

from bson import ObjectId
from bson.errors import InvalidId
from gridfs.errors import NoFile
from postanalysis_api.schemas.metrics import METRICS

FIELDS = {'density': 'density', 'meanSpeed': 'speed', 'pressureProxy': 'pressure_proxy', 'population': 'population'}
BEHAVIORS = ('moving', 'stopped', 'accelerating', 'decelerating')


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def evidence_rows(repository, run, cancelled):
    """Verify owner, run binding, complete hash and bounded JSONL records."""
    artifact = next((row for row in run.get('behaviorEvidence', {}).get('artifacts', [])
                     if row['name'] == 'behavior_evidence.jsonl'), None)
    if artifact is None:
        return
    digest, size = hashlib.sha256(), 0
    with repository.files.open_download_stream(ObjectId(artifact['fileId'])) as stream:
        metadata = stream.metadata or {}
        if any(metadata.get(key) != run.get(field) for key, field in (
            ('ownerId', 'ownerId'), ('workspaceId', 'workspaceId'), ('runId', 'id'))):
            raise ValueError('行为证据的运行或工作区绑定不一致')
        # GridFS iteration is bounded at line boundaries; reject corrupt giant rows.
        for raw in stream:
            cancelled()
            digest.update(raw)
            size += len(raw)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError('单条行为证据超过读取上限')
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError('行为证据行不是对象')
            yield row
    if size != artifact['sizeBytes'] or digest.hexdigest() != artifact['sha256']:
        raise ValueError('行为证据文件SHA256或大小不一致')


def world_input(repository, task, run, checkpoint, *, windows=8):
    """One world is one independent seed. Windows are descriptive observations.

    All evidence rows contribute to counts. Macro values use the final real
    observation in each explicit interval. This is an input aggregation, not
    an increase in the number of independent Monte Carlo samples.
    """
    duration = task['scenario']['durationSeconds']
    count = max(1, min(windows, math.ceil(duration)))
    buckets = [dict(counts=Counter(), decisions=Counter(), proposed=Counter(), executions=Counter(), examples=[], last=None,
                    lastEvidenceTime=None) for _ in range(count)]
    def index(time):
        return min(count - 1, max(0, int(float(time) / duration * count)))
    for point in run.get('timeline', []):
        if finite(point.get('timeSeconds')) and 0 <= point['timeSeconds'] <= duration:
            buckets[index(point['timeSeconds'])]['last'] = point
    records, read_error = 0, None
    try:
        for row in evidence_rows(repository, run, checkpoint):
            records += 1
            kind = row.get('kind')
            at = row.get('timeSeconds', row.get('delivered_at'))
            if not finite(at) or not 0 <= at <= duration:
                continue
            bucket = buckets[index(at)]
            if kind in {'motion', 'message', 'decision'}:
                bucket['lastEvidenceTime'] = max(at, bucket['lastEvidenceTime'] or 0)
            if kind == 'motion':
                if row.get('action') in BEHAVIORS:
                    bucket['counts'][row['action']] += 1
                bucket['counts']['motion_samples'] += 1
            elif kind == 'message':
                bucket['counts']['delivered'] += 1
                bucket['counts']['trusted'] += row.get('trusted') is True
            elif kind == 'decision':
                action = row.get('plan', {}).get('proposed_action')
                execution = row.get('execution', {})
                bucket['proposed'][str(action)] += 1
                bucket['executions'][str(execution.get('status'))] += 1
                bucket['counts']['decision_count'] += 1
                if execution.get('status') == 'applied':
                    bucket['decisions'][str(action)] += 1
                    bucket['counts']['applied_decisions'] += 1
                    if len(bucket['examples']) < 2:
                        context = row.get('context', {})
                        profile = context.get('profile', {})
                        observation = context.get('observation') or {}
                        state = context.get('state', {})
                        bucket['examples'].append({'personId': row['personId'], 'timeSeconds': at,
                            'action': action, 'reason': row.get('plan', {}).get('reason'),
                            'knownEvents': state.get('known_events', {}),
                            'profile': {key: profile[key] for key in ('crowd_role', 'risk_tolerance', 'crowding_tolerance',
                                'patience', 'familiarity', 'mobility', 'following_tendency', 'group_cohesion') if key in profile},
                            'observation': {key: observation[key] for key in ('perceived_crowding', 'objective_density_per_m2') if key in observation},
                            'state': {key: state[key] for key in ('perceived_risk', 'blocked_duration', 'hotspot_last_route_change_time') if key in state},
                            'candidateThresholdInputs': [{key: candidate[key] for key in ('target_id', 'estimated_cost_seconds',
                                'minimum_savings_seconds', 'switch_cooldown_seconds', 'congestion_delay_seconds') if key in candidate}
                                for candidate in context.get('candidates', [])[:3]],
                            'contextSource': 'archived pre-decision input; model parameters, not inferred physical force'})
            # Controller/UI activity never becomes an individual action.
    except (ValueError, KeyError, OSError, InvalidId, NoFile) as exc:
        read_error = str(exc)
        # An unverified artifact must not leave apparently valid partial counts.
        for bucket in buckets:
            bucket.update(counts=Counter(), decisions=Counter(), proposed=Counter(), executions=Counter(), examples=[], lastEvidenceTime=None)
    ticks = []
    for idx, bucket in enumerate(buckets):
        point = bucket['last']
        if point is None and bucket['lastEvidenceTime'] is None:
            continue
        counts = bucket['counts']
        state = {out: point[key] for key, out in FIELDS.items() if point is not None and finite(point.get(key))}
        if records and not read_error:
            state.update({f'agent_{key}_samples': counts[key] for key in BEHAVIORS})
            state.update(delivered_messages=counts['delivered'], trusted_messages=counts['trusted'],
                         applied_decisions=counts['applied_decisions'])
        evidence = {'intervalStartSeconds': duration * idx / count,
                    'intervalEndSeconds': duration * (idx + 1) / count,
                    'macroObservationSeconds': point['timeSeconds'] if point else None, 'individualDecisionCounts': dict(bucket['decisions']),
                    'observedThroughSeconds': max(point['timeSeconds'] if point else 0, bucket['lastEvidenceTime'] or 0),
                    'proposedDecisionCounts': dict(bucket['proposed']), 'executionStatusCounts': dict(bucket['executions']),
                    'individualMotionSampleCounts': {key: counts[key] for key in BEHAVIORS} if not read_error else {},
                    'executionExamples': bucket['examples'], 'exampleLimit': 2,
                    'countUnit': 'person-time sample; not unique people', 'behaviorEvidenceError': read_error}
        ticks.append({'tick_idx': idx, 'timeSeconds': evidence['observedThroughSeconds'], 'camp': 'crowd', 'world_state': state,
                      'decision': {'action': json.dumps(dict(bucket['decisions']), ensure_ascii=False) if bucket['decisions'] else ''},
                      'scenario_content': json.dumps(evidence, ensure_ascii=False, allow_nan=False),
                      'exit': run.get('measurement', {}).get('evacuationCensored') is False and idx == count - 1})
    return {'world_id': run['id'], 'seed': run['seed'], 'finished': run.get('status') == 'completed',
            'error': None if run.get('status') == 'completed' else run.get('error') or run.get('status'),
            'ticks': ticks, 'rv_values': {key: value for key, value in run.get('metrics', {}).items() if finite(value)},
            'rv_snapshots': [], 'behaviorEvidenceStatus': 'invalid' if read_error else 'verified' if records else 'missing',
            'behaviorEvidenceError': read_error, 'sourceRunId': run['id']}


def trajectory_groups(repository, task, runs, checkpoint):
    groups = [{'id': 'F-00', 'name': '事实基线', 'interventions': []}, *task.get('groups', [])]
    result = []
    for group in groups:
        result.append({'group_id': group['id'], 'name': group['name'], 'input_plan': deepcopy(group.get('interventions', [])),
                       'trajectories': [world_input(repository, task, run, checkpoint) for run in runs if run['planId'] == group['id']]})
    return result


def horizontal_input(task, groups):
    # The source factor is the whole declared policy. Single-factor isolation
    # and the actual reference group are separately preserved by paired inference.
    levels = [group['group_id'] for group in groups]
    plans = [{'strategy': key} for key in levels]
    return {'success': True, 'result_query': {'experiment_run_id': task['id'],
        'experiment_design_id': task.get('reviewPlanId'),
        'combination_selection': {'factor_order': ['strategy'], 'factor_levels': {'strategy': levels}},
        'exp_plans': plans,
        'exp_results': [{'input': plans[index], 'trajectories': group['trajectories']} for index, group in enumerate(groups)]}}


def crowd_design():
    return {'world_state_meta': {key: {'label': label} for key, label in (
        ('agent_moving_samples', '个体移动样本'), ('agent_stopped_samples', '个体停止样本'),
        ('agent_accelerating_samples', '个体加速样本'), ('agent_decelerating_samples', '个体减速样本'),
        ('delivered_messages', '引导消息送达'), ('trusted_messages', '引导消息可信'),
        ('applied_decisions', '实际执行决策'), ('density', '区域密度'), ('speed', '平均速度'),
        ('pressure_proxy', '人群压力代理'), ('population', '区域人数'))},
        'rv_binding': {key: {'unit': value['unit']} for key, value in METRICS.items()}}
