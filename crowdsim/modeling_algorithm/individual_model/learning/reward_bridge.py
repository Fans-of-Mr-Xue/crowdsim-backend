"""Explicit interval reward and evaluated-sample bridge, not Runtime enablement.

Only observed risk exposure and actual route-write count are available in v1.
Goal progress / unplanned blocking require additional attribution, never imputed.
"""
from dataclasses import dataclass
from bisect import bisect_right
import json
from .core_contracts import (CoreBinding, LearningSample, action_key, canonical, digest,
                             discrete_state, features, finite)
from .observed_feedback import (ACTION_VERSION, FEATURE_VERSION, INTERVAL_VERSION,
                               EncodedChoice, IntervalEvidence, FeatureEncoder)

RULE_ID = 'observed-interval-reward-v1'
COMPONENTS = {'goal_progress', 'risk_exposure', 'unplanned_blocking', 'switch_cost'}


@dataclass(frozen=True)
class RewardEvaluation:
    payload_json: str
    def as_dict(self):return json.loads(self.payload_json)


class IntervalRewardRule:
    """Weights and unit scales supplied explicitly; missing active terms block samples."""
    def __init__(self, *, weights, scales):
        if not isinstance(weights,dict) or not weights or set(weights)-COMPONENTS:
            raise ValueError('explicit reward weights required')
        if not isinstance(scales,dict) or set(scales)!=set(weights):
            raise ValueError('explicit scale per reward term required')
        checked={k:finite(v,'reward weight',lo=0,hi=100) for k,v in weights.items()}
        if not any(checked.values()):raise ValueError('at least one active reward term required')
        normalized={k:finite(v,'reward scale',lo=1e-6,hi=1e9) for k,v in scales.items()}
        self._spec=canonical(dict(rule_id=RULE_ID,weights=checked,scales=normalized,
            definitions={'risk_exposure':'negative trapezoid perceived-risk seconds',
                'switch_cost':'negative applied route-replacement count; not travel-time loss',
                'goal_progress':'unavailable until goal attribution implemented',
                'unplanned_blocking':'unavailable until blocking attribution implemented'},
            accepted_end_reasons=['decision_boundary','route_stage_completed']))
        self.fingerprint=digest(json.loads(self._spec))

    @property
    def spec(self):return json.loads(self._spec)

    def evaluate(self,evidence):
        if not isinstance(evidence,IntervalEvidence):raise ValueError('frozen interval evidence required')
        row=evidence.as_dict();validate_interval(row,evidence.interval_id)
        duration=finite(row['duration_seconds'],'interval duration',lo=0,hi=1e6)
        if not abs((row['ended_at']-row['started_at'])-duration)<1e-7:
            raise ValueError('interval duration mismatch')
        eligible=(duration>0 and row['end_reason'] in self.spec['accepted_end_reasons']
            and not row['truncated'] and not row['terminated']
            and not row.get('unobserved_tail_seconds',0)
            and not row['before']['constraints'].get('control_limits')
            and not row['last']['constraints'].get('control_limits')
            and not row['activity_seconds'].get('external_control'))
        raw={'risk_exposure':finite(row['risk_exposure_seconds'],'risk integral',lo=0,hi=duration+1e-7),
             'switch_cost':1. if row['action'][0] in {'reroute','change_goal'} else 0.,
             'goal_progress':None,'unplanned_blocking':None}
        terms={};unavailable=[];total=0.;s=self.spec
        for name,weight in s['weights'].items():
            if weight==0:
                terms[name]=dict(weight=0.,scale=s['scales'][name],raw=raw[name],contribution=0.,status='disabled')
                continue
            if raw[name] is None:
                unavailable.append(name);terms[name]=dict(weight=weight,scale=s['scales'][name],raw=None,contribution=None,status='unavailable')
            else:
                value=-weight*raw[name]/s['scales'][name]
                total+=value;terms[name]=dict(weight=weight,scale=s['scales'][name],raw=raw[name],contribution=value,status='observed')
        ready=eligible and not unavailable
        if ready:finite(total,'evaluated reward',lo=-1e6,hi=1e6)
        return RewardEvaluation(canonical(dict(rule_id=RULE_ID,rule_fingerprint=self.fingerprint,
            evidence_id=evidence.interval_id,evidence_digest=digest(row),eligible=eligible,
            unavailable_terms=unavailable,terms=terms,reward=total if ready else None,trainable=ready,
            reason='evaluated' if ready else 'unavailable_terms' if eligible else 'ineligible_interval')))


def validate_interval(row,identity):
    required={'interval_id','version','run_id','model_fingerprint','person_id','role_id','action',
        'plan','receipt','started_at','ended_at','duration_seconds','before','last','mode',
        'end_reason','truncated','terminated','activity_seconds','risk_exposure_seconds','cost_complete'}
    if not isinstance(row,dict) or not required.issubset(row) or row['interval_id']!=identity or row['version']!=INTERVAL_VERSION:
        raise ValueError('unsupported interval contract')
    if not isinstance(row['action'],list):raise ValueError('invalid interval semantic action')
    for name in ('run_id','model_fingerprint','person_id','role_id'):
        if not isinstance(row[name],str) or not row[name]:raise ValueError('invalid interval identity')
    finite(row['duration_seconds'],'duration',lo=0,hi=1e6)
    action=action_key(tuple(row['action']))
    finite(row['started_at'],'interval start',lo=0)
    finite(row['ended_at'],'interval end',lo=row['started_at'])
    if not isinstance(row['activity_seconds'],dict) or set(row['activity_seconds'])-{'moving','low_speed_unclassified',
            'flow_hold','activity_hold','policy_wait','waiting_stage','external_control'}:
        raise ValueError('invalid interval activity categories')
    for value in row['activity_seconds'].values():finite(value,'activity duration',lo=0)
    if abs(sum(row['activity_seconds'].values())-row['duration_seconds'])>1e-7:
        raise ValueError('interval activity coverage mismatch')
    for flag in ('terminated','truncated','cost_complete'):
        if type(row[flag]) is not bool:raise ValueError('invalid interval flags')
    if row['terminated'] and row['truncated']:raise ValueError('conflicting interval termination')
    plan=row['plan'];receipt=row['receipt']
    if (not isinstance(plan,dict) or not {'person_id','snapshot_id','proposed_action','decided_at','candidate_id'}.issubset(plan)
        or not isinstance(receipt,dict) or not {'person_id','proposed_action','applied_action','status','applied_at'}.issubset(receipt)):
        raise ValueError('invalid interval plan/receipt')
    if (receipt['status']!='applied' or receipt['applied_action']!=action[0]
        or receipt['proposed_action']!=action[0] or plan['proposed_action']!=action[0]
        or plan['person_id']!=row['person_id'] or receipt['person_id']!=row['person_id']
        or receipt['applied_at']!=row['started_at'] or plan['decided_at']!=row['started_at']):
        raise ValueError('feedback must match actual applied action')
    for key,t in [('before',row['started_at']),('last',row['ended_at'])]:
        p=row[key]
        if (not isinstance(p,dict) or not {'person_id','role_id','time_seconds','motion','snapshot_id','constraints'}.issubset(p)
            or not isinstance(p['motion'],dict) or not {'person_id','time_seconds'}.issubset(p['motion'])
            or not isinstance(p['constraints'],dict) or not isinstance(p['snapshot_id'],str)):
            raise ValueError('invalid interval observation point')
        controls=p['constraints']
        if (not {'control_limits','flow_hold','activity_hold','policy_wait'}.issubset(controls)
            or not isinstance(controls['control_limits'],dict) or not isinstance(controls['flow_hold'],list)
            or type(controls['activity_hold']) is not bool or type(controls['policy_wait']) is not bool):
            raise ValueError('missing interval control facts')
        for v in controls['control_limits'].values():finite(v,'control limit',lo=0)
        if (p['person_id']!=row['person_id'] or p['role_id']!=row['role_id'] or p['time_seconds']!=t
            or p['motion']['person_id']!=row['person_id'] or p['motion']['time_seconds']!=t
            or not p['snapshot_id'].startswith(row['run_id']+':')):
            raise ValueError('interval point identity/time mismatch')
    if plan['snapshot_id']!=row['before']['snapshot_id']:raise ValueError('interval plan snapshot mismatch')
    if row['mode'] not in {'decision_interval','completed_route'}:raise ValueError('unknown interval mode')
    if ((row['end_reason']=='decision_boundary' and row['mode']!='decision_interval')
        or (row['end_reason']=='route_stage_completed' and (row['mode']!='completed_route' or not row['cost_complete']))):
        raise ValueError('end reason inconsistent with evidence mode')
    if row.get('unobserved_tail_seconds') is not None:
        finite(row['unobserved_tail_seconds'],'unobserved tail',lo=0)
    if row.get('cost_complete') and row.get('unobserved_tail_seconds',0):
        raise ValueError('completed cost includes unobserved tail')


def validate_encoding(encoded,binding,row,*,before):
    if not isinstance(encoded,EncodedChoice):raise ValueError('explicit frozen encoding required')
    e=encoded.as_dict();point=row['before' if before else 'last']
    required={'feature_version','action_version','encoding_fingerprint','schema','person_id',
        'snapshot_id','time_seconds','features','state','action','raw_values','candidate_id'}
    if not isinstance(e,dict) or not required.issubset(e):raise ValueError('incomplete encoded choice')
    if (not isinstance(e['schema'],dict) or not isinstance(e['features'],list)
        or not isinstance(e['state'],list) or not isinstance(e['action'],list)):
        raise ValueError('invalid encoded vectors/schema')
    if (e['feature_version']!=FEATURE_VERSION or e['action_version']!=ACTION_VERSION
        or e['encoding_fingerprint']!=encoded.encoding_fingerprint
        or digest(e['schema'])!=encoded.encoding_fingerprint
        or binding.config['feature_schema_version']!=encoded.encoding_fingerprint
        or binding.config['action_schema_version']!=ACTION_VERSION
        or e['person_id']!=row['person_id'] or e['snapshot_id']!=point['snapshot_id']
        or e['time_seconds']!=point['time_seconds'] or tuple(e['features'])!=encoded.features
        or tuple(e['state'])!=encoded.state or tuple(e['action'])!=encoded.action):
        raise ValueError('sample encoding binding mismatch')
    schema=e['schema'];raw=e.get('raw_values')
    if not isinstance(raw,dict) or not isinstance(schema,dict):raise ValueError('invalid encoded raw values')
    try:
        checked_encoder=FeatureEncoder(columns=tuple(schema['columns']),scales=schema['scales'],
            state_bins={k:tuple(v) for k,v in schema['state_bins'].items()},
            emotion_participates=schema['emotion_participates'],version=schema['version'])
        if checked_encoder.fingerprint!=encoded.encoding_fingerprint:
            raise ValueError('encoding schema mismatch')
        for value in raw.values():finite(value,'encoded raw value')
        vector=tuple(finite(raw[c]/schema['scales'][c],'encoded feature',lo=-1000,hi=1000) for c in schema['columns'])
        state=tuple(bisect_right(edges,raw[c]) for c,edges in schema['state_bins'].items())
    except (KeyError,TypeError,ZeroDivisionError) as exc:
        raise ValueError('invalid encoding schema/raw values') from exc
    if vector!=encoded.features or state!=encoded.state:raise ValueError('encoded vector differs from raw evidence')
    for c,source in [('perceived_risk','perceived_risk'),('perceived_crowding','perceived_crowding'),('objective_density','objective_density')]:
        if c in raw and raw[c]!=point.get(source):raise ValueError('encoded value differs from observation')
    features(encoded.features,binding.dimension);discrete_state(encoded.state)
    binding.validate_target(encoded.action)
    if before and 'candidate_cost' in raw and raw['candidate_cost']!=row.get('predicted_cost_seconds'):
        raise ValueError('encoded candidate cost differs from executed prediction')
    if 'remaining_stages' in raw and raw['remaining_stages']!=point['motion']['remaining_stage_count']:
        raise ValueError('encoded remaining stages mismatch')
    if 'speed_ratio' in raw:
        # Profile ratio cannot be reconstructed from a physical point alone; source encoding remains frozen.
        finite(raw['speed_ratio'],'speed ratio',lo=0)
    if before and tuple(row['action'])[0]=='wait':
        if encoded.action!=('wait',format(row['plan']['wait_until']-row['started_at'],'.12g')):
            raise ValueError('executed wait duration mismatch')
    if before and tuple(row['action'])[0]=='slow_down':
        if encoded.action!=('slow_down',format(row['plan']['speed_limit'],'.12g')):
            raise ValueError('executed speed limit mismatch')
    if encoded.action[0] in {'reroute','change_goal'}:
        if not before and point['motion']['stage_type']!=2:
            raise ValueError('route next action not legal in current waiting stage')
        if not isinstance(e['candidate_id'],str) or not e['candidate_id'].startswith('candidate-'):
            raise ValueError('route encoding missing frozen candidate')
    elif e['candidate_id'] is not None:
        raise ValueError('local encoding contains candidate')
    if before and encoded.action!=tuple(row['action']):raise ValueError('proposed versus executed encoding mismatch')
    if before and e['candidate_id']!=row['plan']['candidate_id']:raise ValueError('executed candidate mismatch')
    return e


def build_evaluated_sample(binding,rule,evidence,before,*,next_choices):
    """Build only supported observed intervals; legal next choices supplied explicitly.

    Does not infer legal actions from a previous frame or train a proposed action.
    Does not mutate the source interval or feed a learner automatically.
    """
    if not isinstance(binding,CoreBinding) or not isinstance(rule,IntervalRewardRule):
        raise ValueError('explicit core binding and reward rule required')
    if not isinstance(evidence,IntervalEvidence):raise ValueError('explicit interval evidence required')
    row=evidence.as_dict();result=rule.evaluate(evidence);evaluation=result.as_dict()
    ident=binding.identity
    if (ident.run_id!=row['run_id'] or ident.model_fingerprint!=row['model_fingerprint']
        or ident.role_id!=row['role_id'] or ident.subject_id!=row['person_id']):
        raise ValueError('interval/core identity mismatch')
    fb=binding.config['feedback']
    if fb['rule_id']!=RULE_ID or fb['version']!=rule.fingerprint or fb['weights']!=rule.spec['weights']:
        raise ValueError('feedback configuration fingerprint mismatch')
    validate_encoding(before,binding,row,before=True)
    if not evaluation['trainable']:return None,result
    choices=tuple(next_choices)
    if not choices or len(choices)>256:raise ValueError('observed nonterminal needs legal next choices')
    for c in choices:validate_encoding(c,binding,row,before=False)
    if len({c.action for c in choices})!=len(choices) or len({c.state for c in choices})!=1 or len({c.encoding_fingerprint for c in choices})!=1:
        raise ValueError('conflicting/duplicate next legal encodings')
    if binding.config['method_id']=='bounded_adaptation_v1' and not row['cost_complete']:
        return None,RewardEvaluation(canonical({**evaluation,'trainable':False,'reason':'completed_cost_required'}))
    cost=row.get('observed_cost_seconds') if row['cost_complete'] else None
    if row['cost_complete']:
        done=row.get('completed_stage')
        needed={'travelTime','type','edges','arrivalPos','description','depart'}
        if not isinstance(done,dict) or not needed.issubset(done) or not {'route_stage_identity','cost_started_at','cost_completed_at'}.issubset(row):
            raise ValueError('unproven completed cost')
        finite(cost,'completed cost',lo=1e-6,hi=1e9)
        finite(done['depart'],'completed departure',lo=row['started_at'])
        finite(row['cost_completed_at'],'completed arrival',lo=row['started_at'],hi=row['ended_at']+1e-7)
        if (row['mode']!='completed_route' or row['end_reason']!='route_stage_completed'
            or done['travelTime']!=cost or done['type']!=2
            or digest({k:done[k] for k in ('type','edges','arrivalPos','description')})!=row['route_stage_identity']
            or done['depart']!=row['cost_started_at'] or done['depart']+cost!=row['cost_completed_at']):
            raise ValueError('unproven completed cost')
    # Envelope binds the schema/scales even if low-level core consumes only versioned fields.
    choices=tuple(sorted(choices,key=lambda c:c.action))
    # One actual interval per binding, even if caller retries with another list order.
    sample_id='sample-'+digest(dict(binding=binding.fingerprint,evidence_id=evidence.interval_id))[7:]
    sample=LearningSample(sample_id,binding.fingerprint,before.action,before.state,before.features,
        evaluation['reward'],row['duration_seconds'],choices[0].state,tuple(c.action for c in choices),
        False,False,RULE_ID,rule.fingerprint,evidence.interval_id,
        row.get('predicted_cost_seconds') if row['cost_complete'] else None,cost,row['cost_complete'],
        row['plan'].get('target_id'),before.as_dict().get('candidate_id'))
    binding.validate(sample)
    return sample,result
