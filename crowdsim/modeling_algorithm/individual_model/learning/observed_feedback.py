"""Versioned, explicit learning inputs and physical-interval evidence.

No reward weights, discretization thresholds or Runtime enablement are inferred.
This module never changes motion, confirmed models, PAD, plans or learner state.
"""
from bisect import bisect_right
from collections import Counter, deque
from dataclasses import asdict, dataclass
import json
import math

from crowdsim.domain.crowdsim_models import (
    AgentProfile, AgentState, Observation, BehaviorPlan, PlanExecutionResult)
from crowdsim.decision.route_provider import bind_candidates, select_candidate
from .core_contracts import action_key, canonical, digest, finite, semantic_route_key, text

FEATURE_VERSION = 'observed-choice-features-v1'
ACTION_VERSION = 'semantic-plan-actions-v1'
INTERVAL_VERSION = 'physical-interval-evidence-v1'
BASE_COLUMNS = {'bias', 'perceived_risk', 'perceived_crowding', 'objective_density',
    'speed_ratio', 'stress', 'fatigue', 'remaining_stages', 'candidate_cost',
    'candidate_entry_density', 'pad_pleasure', 'pad_arousal', 'pad_dominance'}
STATE_COLUMNS = BASE_COLUMNS - {'bias', 'candidate_cost', 'candidate_entry_density'}


@dataclass(frozen=True)
class EncodedChoice:
    action: tuple[str, ...]
    features: tuple[float, ...]
    state: tuple[int, ...]
    encoding_fingerprint: str
    evidence_json: str

    def as_dict(self):
        return json.loads(self.evidence_json)


class FeatureEncoder:
    """Explicit column order, scales and bins; no implicit imputation or clipping."""
    def __init__(self, *, columns, scales, state_bins, emotion_participates, version=FEATURE_VERSION):
        if version != FEATURE_VERSION:
            raise ValueError('unsupported observed feature version')
        if not isinstance(columns, tuple) or not 1 <= len(columns) <= 32:
            raise ValueError('explicit feature columns required')
        if any(not isinstance(c, str) or c not in BASE_COLUMNS for c in columns) or len(set(columns))!=len(columns):
            raise ValueError('unknown or duplicate feature columns')
        if not isinstance(scales, dict) or set(scales)!=set(columns):
            raise ValueError('explicit scale for every feature required')
        if type(emotion_participates) is not bool:
            raise ValueError('explicit emotion participation required')
        if any(c.startswith('pad_') for c in columns) and not emotion_participates:
            raise ValueError('PAD features require participating emotion')
        if not isinstance(state_bins, dict) or not state_bins or len(state_bins)>16:
            raise ValueError('explicit finite state discretization required')
        for c, edges in state_bins.items():
            if c not in STATE_COLUMNS or not isinstance(edges, tuple) or len(edges)>100:
                raise ValueError('unsupported state column or bins')
            if c.startswith('pad_') and not emotion_participates:
                raise ValueError('PAD state requires participating emotion')
            values=tuple(finite(x,'state threshold') for x in edges)
            if values!=tuple(sorted(set(values))):
                raise ValueError('state thresholds must be strictly increasing')
        checked={c:finite(v,'feature scale',lo=1e-6,hi=1e9) for c,v in scales.items()}
        self._schema=canonical(dict(version=version, columns=columns, scales=checked,
            state_bins=state_bins, emotion_participates=emotion_participates))
        self.fingerprint=digest(json.loads(self._schema))

    @property
    def schema(self):
        return json.loads(self._schema)

    def encode(self, profile, state, observation, plan, candidates, *, pad=None):
        observation_values(profile,state,observation)
        candidates=tuple(candidates)
        key=encode_plan_action(plan,observation,candidates)
        selected=select_candidate(candidates,candidate_id=plan.candidate_id,target_id=plan.target_id) if plan.proposed_action in {'reroute','change_goal'} else None
        s=self.schema
        required=set(s['columns'])|set(s['state_bins'])
        values=extract_values(profile,state,observation,selected,pad,required)
        vector=tuple(finite(values[c]/s['scales'][c],'normalized feature',lo=-1000,hi=1000) for c in s['columns'])
        indices=tuple(bisect_right(edges,values[c]) for c,edges in s['state_bins'].items())
        payload=dict(feature_version=FEATURE_VERSION,action_version=ACTION_VERSION,
            encoding_fingerprint=self.fingerprint, schema=s, person_id=profile.person_id,
            snapshot_id=observation.snapshot_id,time_seconds=observation.time_seconds,
            raw_values=values,features=vector,state=indices,action=key,
            candidate_id=selected.candidate_id if selected else None)
        return EncodedChoice(key,vector,indices,self.fingerprint,canonical(payload))


def observation_values(profile,state,observation):
    if not isinstance(profile,AgentProfile) or not isinstance(state,AgentState) or not isinstance(observation,Observation):
        raise ValueError('real profile/state/observation contracts required')
    m=observation.own_motion
    if profile.person_id!=state.person_id or state.person_id!=observation.person_id or m.person_id!=profile.person_id:
        raise ValueError('feature observation person mismatch')
    if m.time_seconds!=observation.time_seconds:
        raise ValueError('stale feature motion')
    finite(observation.time_seconds,'observation time',lo=0)
    text(observation.snapshot_id,'snapshot',limit=300)
    finite(m.speed,'speed',lo=0);finite(m.lane_position,'lane position',lo=0)
    if type(m.stage_type) is not int or m.stage_type not in {1,2} or type(m.remaining_stage_count) is not int or m.remaining_stage_count<1:
        raise ValueError('unknown/unsupported physical stage')
    return m


def extract_values(profile,state,observation,candidate,pad,required):
    m=observation.own_motion
    source=dict(bias=1.,perceived_risk=observation.perceived_risk,
        perceived_crowding=observation.perceived_crowding,objective_density=observation.objective_density_per_m2,
        stress=state.stress,fatigue=state.fatigue,remaining_stages=m.remaining_stage_count)
    if 'speed_ratio' in required:
        free=finite(profile.free_walking_speed,'free speed',lo=1e-6)*finite(profile.mobility,'mobility',lo=1e-6)
        source['speed_ratio']=m.speed/free
    if any(c.startswith('pad_') for c in required):
        if not isinstance(pad,dict) or set(pad)!={'pleasure','arousal','dominance'}:
            raise ValueError('explicit current PAD values required')
        for k,v in pad.items():source['pad_'+k]=finite(v,'PAD',lo=-1,hi=1)
    if candidate is not None:
        source['candidate_cost']=candidate.estimated_cost_seconds
        source['candidate_entry_density']=candidate.entry_density_person_per_m2
    result={}
    for c in sorted(required):
        value=source.get(c)
        if value is None:raise ValueError('unavailable learning feature: '+c)
        lo=-1 if c.startswith('pad_') else 0
        hi=1 if c in {'perceived_risk','perceived_crowding','stress','fatigue'} or c.startswith('pad_') else 1e9
        result[c]=finite(value,c,lo=lo,hi=hi)
    return result


def encode_plan_action(plan,observation,candidates):
    """Stable actual plan semantics; absolute clock/snapshot is not the action key."""
    if not isinstance(plan,BehaviorPlan) or plan.person_id!=observation.person_id or plan.snapshot_id!=observation.snapshot_id:
        raise ValueError('plan/observation mismatch')
    now=observation.time_seconds
    finite(now,'action time',lo=0)
    if plan.expires_at is not None and finite(plan.expires_at,'expires at',lo=0)<now:
        raise ValueError('expired feature action')
    finite(plan.decided_at,'decided at',lo=0)
    if plan.decided_at!=now:raise ValueError('action timestamp mismatch')
    if plan.proposed_action in {'reroute','change_goal'}:
        candidates=tuple(candidates)
        if candidates!=bind_candidates(candidates,plan.person_id,plan.snapshot_id):
            raise ValueError('action needs unmodified frozen candidates')
        c=select_candidate(candidates,candidate_id=plan.candidate_id,target_id=plan.target_id)
        if (plan.route_edges!=c.edges or plan.arrival_position!=c.arrival_position
            or plan.selected_entry_edge!=(c.entry_edge if c.target_kind=='hotspot_route' else None)
            or plan.preserve_future_stages!=(c.target_kind=='hotspot_route')
            or plan.activity_duration!=(c.activity_duration if plan.proposed_action=='change_goal' else None)
            or plan.next_route_edges!=(c.next_route_edges if plan.proposed_action=='change_goal' else ())
            or plan.next_arrival_position!=(c.next_arrival_position if plan.proposed_action=='change_goal' else None)
            or plan.next_target_id!=(c.next_target_id if plan.proposed_action=='change_goal' else None)):
            raise ValueError('action payload differs from candidate')
        return semantic_route_key(plan.proposed_action,c)
    if (plan.candidate_id or plan.route_edges or plan.target_id or plan.next_route_edges
            or plan.activity_duration is not None or plan.arrival_position is not None
            or plan.selected_entry_edge is not None or plan.preserve_future_stages
            or plan.next_arrival_position is not None or plan.next_target_id is not None):
        raise ValueError('local action includes itinerary')
    if plan.proposed_action=='continue':return action_key(('continue',))
    if plan.proposed_action=='wait':
        dt=finite(plan.wait_until,'wait until',lo=0)-now
        finite(dt,'wait duration',lo=1e-6)
        return action_key(('wait',format(dt,'.12g')))
    if plan.proposed_action=='slow_down':
        return action_key(('slow_down',format(finite(plan.speed_limit,'speed limit',lo=0),'.12g')))
    raise ValueError('unsupported semantic action')


@dataclass(frozen=True)
class StageEvidence:
    payload_json: str

    def as_dict(self):return json.loads(self.payload_json)

    @classmethod
    def capture(cls,stage):
        # All fields are read from actual Stage. No inferred arrival or elapsed time.
        fields=('type','edges','arrivalPos','depart','departPos','travelTime','description')
        raw={name:getattr(stage,name) for name in fields}
        if type(raw['type']) is not int or raw['type'] not in {1,2}:
            raise ValueError('unsupported stage evidence')
        if not isinstance(raw['edges'],(tuple,list)) or not raw['edges'] or any(not isinstance(e,str) or not e for e in raw['edges']):
            raise ValueError('invalid stage edges')
        for k in ('arrivalPos','depart','departPos','travelTime'):finite(raw[k],k,lo=-1e12,hi=1e12)
        if not isinstance(raw['description'],str):raise ValueError('invalid stage description')
        return cls(canonical(raw))

    def identity(self):
        row=self.as_dict()
        # Timing fields change as SUMO starts/completes a stage; itinerary identity doesn't.
        return digest({k:row[k] for k in ('type','edges','arrivalPos','description')})


@dataclass(frozen=True)
class ObservationPoint:
    payload_json: str

    def as_dict(self):return json.loads(self.payload_json)

    @classmethod
    def capture(cls,profile,state,observation,*,constraints,stage):
        observation_values(profile,state,observation)
        if (not isinstance(constraints,dict) or not {'flow_hold','activity_hold','policy_wait','control_limits'}.issubset(constraints)
            or type(constraints['activity_hold']) is not bool or type(constraints['policy_wait']) is not bool
            or not isinstance(constraints['flow_hold'],list) or not isinstance(constraints['control_limits'],dict)):
            raise ValueError('complete physical control/hold facts required')
        if (any(not isinstance(reason,str) or not reason for reason in constraints['flow_hold'])
                or any(not isinstance(name,str) or not name for name in constraints['control_limits'])):
            raise ValueError('invalid physical control/hold identifiers')
        for value in constraints['control_limits'].values():finite(value,'control limit',lo=0)
        if not isinstance(stage,StageEvidence):
            raise ValueError('explicit current stage evidence required')
        if stage.as_dict()['type']!=observation.own_motion.stage_type:
            raise ValueError('observation stage mismatch')
        raw=dict(person_id=profile.person_id,role_id=profile.crowd_role,snapshot_id=observation.snapshot_id,
            time_seconds=observation.time_seconds,motion=asdict(observation.own_motion),
            perceived_risk=observation.perceived_risk,perceived_crowding=observation.perceived_crowding,
            objective_density=observation.objective_density_per_m2,constraints=constraints,
            stage=stage.as_dict(),stage_identity=stage.identity())
        finite(raw['perceived_risk'],'risk',lo=0,hi=1);finite(raw['perceived_crowding'],'crowding',lo=0,hi=1)
        if raw['objective_density'] is not None:finite(raw['objective_density'],'density',lo=0)
        return cls(canonical(raw))


def waiting_class(point,low_speed_threshold):
    c=point['constraints'];m=point['motion']
    if c.get('control_limits'):return 'external_control'
    if c.get('flow_hold'):return 'flow_hold'
    if c.get('activity_hold'):return 'activity_hold'
    if c.get('policy_wait'):return 'policy_wait'
    if m['stage_type']==1:return 'waiting_stage'
    return 'low_speed_unclassified' if m['speed']<=low_speed_threshold else 'moving'


@dataclass(frozen=True)
class IntervalEvidence:
    interval_id: str
    payload_json: str

    def as_dict(self):return json.loads(self.payload_json)


class PhysicalIntervalJournal:
    """Independent evidence collector, explicit physical-step and boundary semantics.

    Trapezoidal observed risk/crowding/density integrals are numerical measurements,
    not rewards. Hold categories use left-end constraints of the physical step.
    Complete cost needs a matching completed SUMO Stage; snapshots alone never suffice.
    """
    def __init__(self,run_id,*,model_fingerprint,step_seconds,low_speed_threshold,capacity):
        text(run_id,'run');text(model_fingerprint,'model fingerprint')
        finite(step_seconds,'physical step',lo=1e-6,hi=1000)
        finite(low_speed_threshold,'low-speed threshold',lo=0,hi=100)
        if type(capacity) is not int or not 1<=capacity<=100000:raise ValueError('invalid interval capacity')
        self.run_id=run_id;self.model_fingerprint=model_fingerprint
        self.step=step_seconds;self.low=low_speed_threshold;self.capacity=capacity
        self.pending={};self.ready=deque();self.seen=set();self._seen_order=deque();self.counts=Counter()

    @property
    def active_count(self):
        """In-flight intervals: pending (open) plus ready (finished, awaiting drain).

        This is the bounded resource the capacity guard must constrain. `seen` is
        a bounded dedup window, not active backlog, and must not exhaust capacity.
        """
        return len(self.pending)+len(self.ready)

    def _point(self,point):
        if not isinstance(point,ObservationPoint):raise ValueError('explicit observation point required')
        row=point.as_dict()
        if not row['snapshot_id'].startswith(self.run_id+':'):raise ValueError('interval point from another run')
        return row

    def begin(self,interval_id,point,plan,receipt,candidates,*,mode):
        text(interval_id,'interval id')
        row=self._point(point)
        if mode not in {'decision_interval','completed_route'}:raise ValueError('explicit interval mode required')
        if not isinstance(receipt,PlanExecutionResult) or not isinstance(plan,BehaviorPlan):raise ValueError('explicit plan/receipt required')
        if (receipt.person_id!=row['person_id'] or plan.person_id!=row['person_id'] or plan.snapshot_id!=row['snapshot_id']
            or receipt.proposed_action!=plan.proposed_action or receipt.applied_at!=row['time_seconds']):
            raise ValueError('interval execution binding mismatch')
        if receipt.status!='applied' or receipt.applied_action!=plan.proposed_action:
            raise ValueError('unapplied proposal cannot start training interval')
        if interval_id in self.seen:
            self.counts['duplicates']+=1
            return 'duplicate'
        if self.active_count>=self.capacity:raise BufferError('interval capacity reached')
        if any(x['person_id']==row['person_id'] for x in self.pending.values()):raise ValueError('previous person interval must be closed first')
        if row['constraints'].get('control_limits'):raise ValueError('external control interval cannot start')
        if plan.expires_at is not None and finite(plan.expires_at,'expires at',lo=0)<row['time_seconds']:
            raise ValueError('interval expired plan')
        finite(plan.decided_at,'action time',lo=0)
        if plan.decided_at!=row['time_seconds']:raise ValueError('interval stale plan time')
        route=plan.proposed_action in {'reroute','change_goal'}
        if mode=='completed_route' and not route:raise ValueError('route interval requires a route action')
        selected=None
        if route:
            cs=tuple(candidates)
            if cs!=bind_candidates(cs,plan.person_id,plan.snapshot_id):raise ValueError('invalid interval candidate binding')
            selected=select_candidate(cs,candidate_id=plan.candidate_id,target_id=plan.target_id)
            if (plan.route_edges!=selected.edges or plan.arrival_position!=selected.arrival_position
                or plan.selected_entry_edge!=(selected.entry_edge if selected.target_kind=='hotspot_route' else None)
                or plan.preserve_future_stages!=(selected.target_kind=='hotspot_route')
                or plan.activity_duration!=(selected.activity_duration if plan.proposed_action=='change_goal' else None)
                or plan.next_route_edges!=(selected.next_route_edges if plan.proposed_action=='change_goal' else ())
                or plan.next_arrival_position!=(selected.next_arrival_position if plan.proposed_action=='change_goal' else None)
                or plan.next_target_id!=(selected.next_target_id if plan.proposed_action=='change_goal' else None)):
                raise ValueError('interval route payload mismatch')
            stage=row['stage']
            if stage['type']!=2 or tuple(stage['edges'])!=selected.edges or not math.isclose(stage['arrivalPos'],selected.arrival_position,abs_tol=1e-4):
                raise ValueError('executed stage differs from selected route')
            key=semantic_route_key(plan.proposed_action,selected)
        else:
            if (plan.candidate_id or plan.route_edges or plan.target_id or plan.activity_duration is not None or plan.next_route_edges
                or plan.arrival_position is not None or plan.selected_entry_edge is not None or plan.preserve_future_stages
                or plan.next_arrival_position is not None or plan.next_target_id is not None):
                raise ValueError('local interval action includes route')
            if plan.proposed_action=='continue':key=('continue',)
            elif plan.proposed_action=='wait':
                duration=finite(plan.wait_until,'wait until',lo=0)-row['time_seconds']
                finite(duration,'wait duration',lo=1e-6)
                key=('wait',format(duration,'.12g'))
            elif plan.proposed_action=='slow_down':key=('slow_down',format(finite(plan.speed_limit,'speed limit',lo=0),'.12g'))
            else:raise ValueError('unsupported interval action')
        action_key(key)
        entry=dict(interval_id=interval_id,version=INTERVAL_VERSION,mode=mode,run_id=self.run_id,
            model_fingerprint=self.model_fingerprint,person_id=row['person_id'],role_id=row['role_id'],
            action=key,plan=asdict(plan),receipt=asdict(receipt),started_at=row['time_seconds'],
            before=row,last=row,step_count=0,risk_exposure_seconds=0.,crowding_exposure_seconds=0.,
            density_exposure_person_seconds_per_m2=0.,density_available=True,activity_seconds={},
            quadrature='trapezoid_observation_endpoints',activity_quadrature='left_endpoint_constraints',
            unplanned_blocking_seconds=None,goal_progress=None,
            predicted_cost_seconds=selected.estimated_cost_seconds if selected else None,
            route_stage_identity=row['stage_identity'] if route else None)
        self.pending[interval_id]=entry;self.seen.add(interval_id);self._seen_order.append(interval_id)
        while len(self._seen_order)>self.capacity:
            self.seen.discard(self._seen_order.popleft())
        self.counts['started']+=1
        return 'started'

    def advance(self,interval_id,point,*,decision_boundary=False,timeline_end=False,completed_stage=None):
        if interval_id not in self.pending:raise ValueError('unknown interval')
        if (type(decision_boundary) is not bool or type(timeline_end) is not bool):
            raise ValueError('explicit boolean interval boundaries required')
        row=self._point(point);old=self.pending[interval_id]
        if row['person_id']!=old['person_id'] or row['role_id']!=old['role_id']:raise ValueError('interval person/role changed')
        dt=row['time_seconds']-old['last']['time_seconds']
        if dt==0:
            if row==old['last']:return 'duplicate'
            raise ValueError('conflicting same-time observation')
        if row['snapshot_id']==old['last']['snapshot_id'] or not math.isclose(dt,self.step,rel_tol=0,abs_tol=1e-7):
            raise ValueError('interval skipped or stale physical observation')
        # Work on detached state; failed validation cannot leave half an integral.
        item=json.loads(canonical(old));previous=item['last'];item['last']=row;item['step_count']+=1
        item['risk_exposure_seconds']+=.5*(previous['perceived_risk']+row['perceived_risk'])*dt
        item['crowding_exposure_seconds']+=.5*(previous['perceived_crowding']+row['perceived_crowding'])*dt
        densities=(previous['objective_density'],row['objective_density'])
        if None in densities:item['density_available']=False
        else:item['density_exposure_person_seconds_per_m2']+=.5*sum(densities)*dt
        label=waiting_class(previous,self.low)
        item['activity_seconds'][label]=item['activity_seconds'].get(label,0)+dt
        if previous['constraints'].get('control_limits') or row['constraints'].get('control_limits'):
            return self._finish(item,'external_control',truncated=True)
        if item['mode']=='completed_route':
            if row['stage_identity']!=item['route_stage_identity']:
                if not isinstance(completed_stage,StageEvidence):
                    return self._finish(item,'stage_change_without_completion',truncated=True)
                done=completed_stage.as_dict()
                if completed_stage.identity()!=item['route_stage_identity'] or done['type']!=2:
                    return self._finish(item,'stage_completion_mismatch',truncated=True)
                depart=finite(done['depart'],'completed depart',lo=0)
                travel=finite(done['travelTime'],'completed travel time',lo=1e-6)
                # Must describe this executed route, not an older walk or the whole original trip.
                if depart<item['started_at']-1e-7 or not previous['time_seconds']-1e-7<=depart+travel<=row['time_seconds']+1e-7:
                    return self._finish(item,'stage_completion_time_mismatch',truncated=True)
                item['completed_stage']=done;item['observed_cost_seconds']=travel
                item['cost_started_at']=depart;item['cost_completed_at']=depart+travel
                return self._finish(item,'route_stage_completed',cost_complete=True)
            if decision_boundary:
                # Observation boundaries alone never terminate a full route.
                pass
        if timeline_end:return self._finish(item,'timeline_end',truncated=True)
        if decision_boundary and item['mode']=='decision_interval':return self._finish(item,'decision_boundary')
        self.pending[interval_id]=item
        return 'pending'

    def _finish(self,item,reason,*,truncated=False,terminated=False,cost_complete=False):
        if len(self.ready)>=self.capacity:raise BufferError('interval ready capacity reached')
        item=json.loads(canonical(item));item['ended_at']=item['last']['time_seconds']
        item.update(end_reason=reason,truncated=truncated,terminated=terminated,cost_complete=cost_complete,
            reward=None,trainable=False,learning_update=None,duration_seconds=item['ended_at']-item['started_at'])
        if not item['density_available']:item['density_exposure_person_seconds_per_m2']=None
        evidence=IntervalEvidence(item['interval_id'],canonical(item))
        self.ready.append(evidence);self.pending.pop(item['interval_id'],None);self.counts['completed']+=1
        return reason

    def close_decision_interval(self, interval_id, point):
        """Close at an already sampled decision boundary without duplicating dt."""
        if interval_id not in self.pending:raise ValueError('unknown interval')
        item=self.pending[interval_id];row=self._point(point)
        if item['mode']!='decision_interval' or row!=item['last'] or item['step_count']<1:
            raise ValueError('decision close requires its latest physical observation')
        return self._finish(item,'decision_boundary')

    def interrupt(self,interval_id,*,reason,now):
        if reason not in {'runtime_closed','runtime_reset','runtime_error','external_control',
            'plan_superseded','explicit_removal','unknown_disappearance','sumo_arrived_without_stage',
            'missing_observation'}:raise ValueError('unknown interval interruption reason')
        if interval_id not in self.pending:return 'not_pending'
        item=json.loads(canonical(self.pending[interval_id]))
        finite(now,'interruption time',lo=item['last']['time_seconds'])
        item['interrupted_at']=now
        item['unobserved_tail_seconds']=now-item['last']['time_seconds']
        return self._finish(item,reason,
            truncated=reason!='sumo_arrived_without_stage', terminated=reason=='sumo_arrived_without_stage')

    def batch(self):return tuple(self.ready)

    def acknowledge(self,batch):
        batch=tuple(batch)
        if tuple(self.ready)[:len(batch)]!=batch:raise ValueError('interval acknowledgement order mismatch')
        for _ in batch:self.ready.popleft()

    def diagnostics(self):
        return dict(version=INTERVAL_VERSION,pending=len(self.pending),unpersisted=len(self.ready),
            active=self.active_count,capacity=self.capacity,
            dedup_window=len(self.seen),**dict(self.counts),
            policy_updates=0,runtime_ready=False)
