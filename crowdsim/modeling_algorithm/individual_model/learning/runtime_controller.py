"""Explicit per-role learning controller attached to the original Runtime.

Integration modes (additive; ``baseline_route_refinement`` is the unchanged v1
same-family refinement):

- ``baseline_route_refinement``: route alternatives keep the baseline goal/arrival,
  local alternatives keep the itinerary untouched.
- ``candidate_scoring``: the learner scores every legal supplied goal/route and
  local candidate; the rule/decision owner still produces the final plan, and the
  baseline may be refined to a top-scoring legal candidate.
- ``llm_baseline_refine``: the LLM remains decision owner; learning refines its
  legal baseline suggestion within the supplied candidate set and feeds scores
  back through the normal accept -> reward -> next-decision chain.

The controller never unlocks itinerary locks, activity dwell, onward chains,
fixed destinations or unreachable/unknown targets, and never bypasses executor
validation. This is an explicit Python integration interface, not product
contract readiness.
"""
from collections import deque
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math

from crowdsim.domain.crowdsim_models import BehaviorPlan
from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
from .configuration import validate_learning_config
from .core_contracts import LearnerIdentity, LegalChoice, canonical, digest, finite
from .learner_cores import create_learner_core
from .observed_feedback import (ACTION_VERSION, FeatureEncoder, ObservationPoint,
                               PhysicalIntervalJournal, StageEvidence)
from .reward_bridge import RULE_ID, IntervalRewardRule, build_evaluated_sample

VERSION='role-learning-controller-v1'

_ROUTE_FAMILY={'reroute','change_goal'}
_LOCAL_FAMILY={'continue','slow_down','wait'}
_BASELINE_MODE='baseline_route_refinement'
_CANDIDATE_SCORING='candidate_scoring'
_LLM_REFINE='llm_baseline_refine'
_INTEGRATION_MODES={_BASELINE_MODE,_CANDIDATE_SCORING,_LLM_REFINE}
# Modes in which the learner may choose among non-locked supplied goal candidates.
_GOAL_SCORING_MODES={_CANDIDATE_SCORING,_LLM_REFINE}
_LLM_SOURCES={'llm','llm_request'}


class RoleLearningController:
    def __init__(self, role_settings, *, capacity):
        if not isinstance(role_settings,dict) or not role_settings:
            raise ValueError('explicit role learning settings required')
        if type(capacity) is not int or not 16<=capacity<=100000:
            raise ValueError('invalid controller capacity')
        configs={}
        for role,setting in role_settings.items():
            if not isinstance(role,str) or not role or not isinstance(setting,dict) or set(setting)!={
                'learning_config','encoding','reward_scales','interval_mode','integration_mode',
                'storage_capacity','bootstrap_on_truncation'}:
                raise ValueError('incomplete/unknown role learning controller settings')
            if setting['integration_mode'] not in _INTEGRATION_MODES:
                raise ValueError('unsupported learning integration mode')
            if setting['interval_mode'] not in {'decision_interval','completed_route'}:
                raise ValueError('unsupported learning interval mode')
            if setting['bootstrap_on_truncation'] is not False:
                raise ValueError('controller v1 does not create truncated bootstrap samples')
            conf=validate_learning_config(setting['learning_config'],complete=True)
            enc=setting['encoding']
            if not isinstance(enc,dict) or set(enc)!={'columns','scales','state_bins','emotion_participates'}:
                raise ValueError('explicit encoder settings required')
            encoder=FeatureEncoder(columns=tuple(enc['columns']),scales=enc['scales'],
                state_bins={k:tuple(v) for k,v in enc['state_bins'].items()},emotion_participates=enc['emotion_participates'])
            rule=IntervalRewardRule(weights=conf['feedback']['weights'],scales=setting['reward_scales'])
            if (conf['feature_schema_version']!=encoder.fingerprint or conf['action_schema_version']!=ACTION_VERSION
                or conf['feedback']['rule_id']!=RULE_ID or conf['feedback']['version']!=rule.fingerprint):
                raise ValueError('controller encoding/feedback configuration mismatch')
            if any(k in encoder.schema['columns'] for k in ('candidate_cost','candidate_entry_density')):
                raise ValueError('controller v1 requires state features usable also at local action boundaries')
            if any(conf['feedback']['weights'].get(k,0)>0 for k in ('goal_progress','unplanned_blocking')):
                raise ValueError('unsupported active feedback attribution')
            if setting['interval_mode']=='completed_route' and 'action' in conf['targets']:
                raise ValueError('completed_route interval records local actions as monitor_only; action targets require decision_interval evidence')
            if setting['interval_mode']=='completed_route' and 'goal_route' not in conf['targets']:
                raise ValueError('completed route requires an explicit route goal target')
            if conf['method_id']=='bounded_adaptation_v1' and setting['interval_mode']!='completed_route':
                raise ValueError('cost adaptation requires completed route')
            # Factory validation proves actual support, without publishing/activating any product model.
            create_learner_core(conf,LearnerIdentity('preflight','sha256:preflight',role,'preflight'),
                feature_dimension=len(encoder.schema['columns']),time_unit_seconds=1,
                bootstrap_on_truncation=setting['bootstrap_on_truncation'],storage_capacity=setting['storage_capacity'])
            configs[role]=dict(setting=deepcopy(setting),encoder=encoder,rule=rule)
        self.capacity=capacity;self.roles=configs
        self.settings_json=canonical(role_settings);self.fingerprint=digest(json.loads(self.settings_json))
        self.learners={};self.contexts={};self.prepared={};self.ready=deque()
        self.sequence=0;self.interval_sequence=0;self.run_id=None;self.model_fingerprint=None;self.step=None
        self.journal=None;self.accepted_samples=0;self.actual_updates=0;self.closed=False
        self.product_contract_ready=False
        self.selection_count=0
        self.selection_by_person={}

    def attach(self,runtime,*,model_fingerprint):
        if runtime.decision_engine.execution_contract not in {'candidate-decision-v1','heterogeneous-learning-v2'}:
            raise ValueError('learning controller requires unique-candidate execution contract')
        finite(runtime.step_length,'runtime step',lo=1e-6)
        from .core_contracts import text
        text(model_fingerprint,'runtime model fingerprint')
        assemblies=runtime.decision_engine.assemblies
        for role,data in self.roles.items():
            assembly=assemblies.get(role)
            if assembly is None or not assembly.learning_requested:
                raise ValueError('controller role learning capability not confirmed')
            coop=data['setting']['integration_mode']==_LLM_REFINE
            if (assembly.decision is not None or runtime.use_llm) and not coop:
                raise ValueError('learning controller does not override or advise this decision owner')
            if coop and assembly.decision is not None and not isinstance(assembly.decision, LLMDrivenDecisionModel):
                raise ValueError('llm_baseline_refine requires an LLM decision assembly')
            if data['encoder'].schema['emotion_participates'] and not assembly.emotion_participates:
                raise ValueError('controller PAD differs from role participation')
        if self.run_id is not None:raise ValueError('controller instance already attached')
        journal=PhysicalIntervalJournal(runtime.run_id,model_fingerprint=model_fingerprint,
            step_seconds=runtime.step_length,low_speed_threshold=runtime.visual_policy.low_speed_threshold_mps,
            capacity=self.capacity)
        self.run_id=runtime.run_id;self.model_fingerprint=model_fingerprint;self.step=runtime.step_length
        self.journal=journal
        self.product_contract_ready=runtime.decision_engine.execution_contract=='heterogeneous-learning-v2'

    def _emit(self,kind,**values):
        if len(self.ready)>=self.capacity:raise BufferError('learning audit capacity reached')
        sequence=self.sequence+1
        payload=canonical(dict(version=VERSION,run_id=self.run_id,controller_fingerprint=self.fingerprint,
            record_id=f'{self.run_id}:learning:{sequence}',kind=kind,**values))
        self.ready.append(payload);self.sequence=sequence

    def _check(self,runtime):
        if self.closed or runtime.run_id!=self.run_id:
            raise ValueError('closed or old-run learning controller')

    def _person(self,runtime,pid):
        profile=runtime.population.profile_for(pid);role=profile.crowd_role
        if role not in self.roles:return None
        if pid not in self.learners:
            if len(self.learners)>=self.capacity:raise BufferError('individual learner capacity reached')
            data=self.roles[role]
            self.learners[pid]=create_learner_core(data['setting']['learning_config'],
                LearnerIdentity(self.run_id,self.model_fingerprint,role,pid),
                feature_dimension=len(data['encoder'].schema['columns']),time_unit_seconds=self.step,
                bootstrap_on_truncation=data['setting']['bootstrap_on_truncation'],
                storage_capacity=data['setting']['storage_capacity'])
        if self.learners[pid].binding.identity.role_id!=role:raise ValueError('learner role changed during run')
        return self.learners[pid]

    def _pad(self,runtime,pid,role):
        if not self.roles[role]['encoder'].schema['emotion_participates']:return None
        context=runtime.decision_engine._boundary_contexts.get(pid)
        if not context or context[0]!=(runtime.snapshot_id,runtime.time_seconds):
            runtime.decision_engine._prepare_affect(runtime.population.profile_for(pid),
                runtime.population.states[pid],runtime.observations[pid])
        value=runtime.decision_engine.emotional_states.get(pid)
        if value is None:raise ValueError('current PAD unavailable')
        return value.as_dict()

    def _encode(self,runtime,plan,candidates):
        pid=plan.person_id;p=runtime.population.profile_for(pid)
        return self.roles[p.crowd_role]['encoder'].encode(p,runtime.population.states[pid],
            runtime.observations[pid],plan,candidates,pad=self._pad(runtime,pid,p.crowd_role))

    def _point(self,runtime,pid):
        p=runtime.population.profile_for(pid)
        return ObservationPoint.capture(p,runtime.population.states[pid],runtime.observations[pid],
            constraints=runtime._post_action_constraints(pid),stage=StageEvidence.capture(runtime.adapter.current_person_stage(pid)))

    def _local_choice_allowed(self,runtime,pid,constraints):
        # Fixed itinerary, activity dwell, policy wait, flow hold and external control
        # never become learning alternatives; the baseline plan is retained unchanged.
        if constraints.get('control_limits') or constraints.get('activity_hold') or constraints.get('policy_wait'):
            return False
        if constraints.get('flow_hold'):
            return False
        state=runtime.population.states[pid]
        if state.poi_plan_active or state.activity_state in {'hotspot_dwelling','hotspot_departing'}:
            return False
        return True

    def _local_alternative(self,runtime,baseline,profile,observation,action):
        now=observation.time_seconds
        if action=='slow_down':
            speed_limit=max(0.2,profile.free_walking_speed*profile.mobility*0.65)
            wait_until=None
        elif action=='wait':
            speed_limit=None
            scheduler=getattr(runtime,'decision_scheduler',None)
            period=getattr(scheduler,'period_seconds',2.0) if scheduler is not None else 2.0
            wait_until=now+max(0.5,period)
        else:
            speed_limit=None;wait_until=None
        return replace(baseline,proposed_action=action,target_id=None,route_edges=(),
            speed_limit=speed_limit,wait_until=wait_until,activity_duration=None,next_route_edges=(),
            arrival_position=None,next_arrival_position=None,next_target_id=None,
            selected_entry_edge=None,preserve_future_stages=False,candidate_id=None)

    def _plans(self,runtime,baseline):
        pid=baseline.person_id;profile=runtime.population.profile_for(pid)
        observation=runtime.observations[pid];motion=runtime.current.persons[pid]
        candidates=runtime.decision_candidates.get(pid,())
        try:
            runtime.plan_executor.validate_plan(baseline,motion,runtime.snapshot_id,runtime.time_seconds,
                candidates=candidates,require_candidate=True)
            self._encode(runtime,baseline,candidates)
        except ValueError:return []
        plans=[baseline]
        locked=pid in runtime.population.locked_itinerary_ids
        constraints=runtime._post_action_constraints(pid)
        role=profile.crowd_role;mode=self.roles[role]['setting']['integration_mode']
        if baseline.proposed_action in _ROUTE_FAMILY and not locked:
            base_candidate=next((x for x in candidates if x.candidate_id==baseline.candidate_id),None)
            if base_candidate is None:return []
            if mode in _GOAL_SCORING_MODES:
                # Goal-scoring modes may choose among the supplied non-locked legal
                # activity goals; hotspot reroute keeps the fixed destination and
                # future stages. Executor/encoding/target validation still gate each.
                def legal_route(c):
                    if c.target_kind!=base_candidate.target_kind:return False
                    if base_candidate.target_kind=='hotspot_route':
                        return c.target_id==base_candidate.target_id
                    return baseline.proposed_action!='change_goal' or c.target_kind=='activity'
            else:
                def legal_route(c):
                    return (c.target_id==base_candidate.target_id and c.target_kind==base_candidate.target_kind
                        and c.target_edge==base_candidate.target_edge and c.arrival_position==base_candidate.arrival_position
                        and (baseline.proposed_action!='change_goal' or (c.activity_duration==base_candidate.activity_duration
                            and c.next_route_edges==base_candidate.next_route_edges and c.next_arrival_position==base_candidate.next_arrival_position
                            and c.next_target_id==base_candidate.next_target_id)))
            plans=[replace(LLMDrivenDecisionModel._make_plan(profile,observation,baseline.proposed_action,c,
                reason=baseline.reason,confidence=baseline.confidence,source=baseline.source),
                scheduling_source=baseline.scheduling_source,expires_at=baseline.expires_at)
                for c in candidates if legal_route(c)]
        elif baseline.proposed_action in _LOCAL_FAMILY and not locked and self._local_choice_allowed(runtime,pid,constraints):
            for action in ('continue','slow_down','wait'):
                if action!=baseline.proposed_action:
                    plans.append(self._local_alternative(runtime,baseline,profile,observation,action))
        valid=[];seen_actions=set()
        for plan in plans:
            try:
                runtime.plan_executor.validate_plan(plan,motion,runtime.snapshot_id,runtime.time_seconds,
                    candidates=candidates,require_candidate=True)
                encoded=self._encode(runtime,plan,candidates)
                self.learners[pid].binding.validate_target(encoded.action)
                if encoded.action in seen_actions:continue
                seen_actions.add(encoded.action)
                valid.append((plan,encoded))
            except ValueError:
                continue
        return valid

    def _completed_route_successors(self, runtime, pid):
        """Legal goal_route successor choices at a completed-route terminal boundary.

        Only supplied candidates that pass executor, encoding and target gating
        are encoded as next choices. No continue/local arm is fabricated as a
        route successor; an empty result means the interval stays unavailable.
        """
        profile = runtime.population.profile_for(pid)
        observation = runtime.observations[pid]
        motion = runtime.current.persons[pid]
        candidates = runtime.decision_candidates.get(pid, ())
        choices, seen = [], set()
        for candidate in candidates:
            action = 'change_goal' if candidate.target_kind == 'activity' else 'reroute'
            plan = LLMDrivenDecisionModel._make_plan(
                profile, observation, action, candidate,
                reason='completed-route successor candidate', confidence=None,
                source='configured_rule')
            try:
                runtime.plan_executor.validate_plan(plan, motion, runtime.snapshot_id,
                    runtime.time_seconds, candidates=candidates, require_candidate=True)
                encoded = self._encode(runtime, plan, candidates)
                learner = self._person(runtime, pid)
                if learner is None:
                    continue
                learner.binding.validate_target(encoded.action)
            except ValueError:
                continue
            if encoded.action in seen:
                continue
            seen.add(encoded.action)
            choices.append(encoded)
        return tuple(choices)

    def _drain_intervals(self):
        batch=self.journal.batch()
        if len(self.ready)+len(batch)>self.capacity:raise BufferError('learning audit capacity before interval drain')
        for evidence in batch:
            pid=evidence.as_dict()['person_id']
            context=self.contexts.get(pid)
            if context and context['interval_id']==evidence.interval_id:
                context['finished']=evidence
            self._emit('interval',evidence=evidence.as_dict())
        self.journal.acknowledge(batch)

    def _update(self,runtime,pid,next_choices):
        context=self.contexts.get(pid)
        if not context or 'finished' not in context:return
        if len(self.ready)>=self.capacity:raise BufferError('learning audit capacity before update')
        learner=self.learners[pid];role=learner.binding.identity.role_id
        # Expected unavailability is an explicit no-update receipt, not silent success.
        try:
            sample,evaluation=build_evaluated_sample(learner.binding,self.roles[role]['rule'],
                context['finished'],context['encoded'],next_choices=next_choices)
        except ValueError as exc:
            self._emit('sample_unavailable',person_id=pid,reason=str(exc),interval_id=context['interval_id'])
        else:
            if sample is None:
                self._emit('sample_unavailable',person_id=pid,evaluation=evaluation.as_dict())
            else:
                # Full update transaction includes append, so failure never leaves a hidden pending sample.
                checkpoint=deepcopy(learner.__dict__)
                try:
                    accepted=learner.observe(sample)
                    updates=learner.update(token=context['interval_id'])
                    self._emit('learning_update',person_id=pid,interval_id=context['interval_id'],
                        snapshot_id=runtime.snapshot_id,time_seconds=runtime.time_seconds,
                        sample=asdict(sample),evaluation=evaluation.as_dict(),
                        before_encoding=context['encoded'].as_dict(),next_encodings=[e.as_dict() for e in next_choices],
                        acceptance=accepted,updates=[u.as_dict() for u in updates])
                except Exception:
                    learner.__dict__.clear();learner.__dict__.update(checkpoint);raise
                self.accepted_samples+=int(accepted=='accepted');self.actual_updates+=len(updates)
        del self.contexts[pid]

    def _role_mode(self,runtime,pid):
        role=runtime.population.profile_for(pid).crowd_role
        data=self.roles.get(role)
        return role,(data['setting']['integration_mode'] if data else None)

    def _tracked_sources(self,mode):
        sources={'configured_rule','budget_fallback'}
        if mode==_LLM_REFINE:sources|=_LLM_SOURCES
        return sources

    def tracks_source(self,runtime,pid,source):
        _,mode=self._role_mode(runtime,pid)
        return source in self._tracked_sources(mode)

    def refine(self,runtime,baseline):
        self._check(runtime);pid=baseline.person_id
        learner=self._person(runtime,pid)
        if learner is None:return baseline
        _,mode=self._role_mode(runtime,pid)
        if baseline.scheduling_source not in self._tracked_sources(mode):return baseline
        if runtime._post_action_constraints(pid).get('control_limits'):
            self.interrupt(runtime,'external_control',person_ids=[pid])
            return baseline
        if len(self.ready)>self.capacity-8:raise BufferError('insufficient learning audit capacity before execution')
        valid=self._plans(runtime,baseline);next_choices=tuple(e for p,e in valid)
        context=self.contexts.get(pid)
        if context and context['interval_id'] in self.journal.pending:
            if self.roles[learner.binding.identity.role_id]['setting']['interval_mode']=='decision_interval':
                latest=ObservationPoint(canonical(self.journal.pending[context['interval_id']]['last']))
                if self.journal.pending[context['interval_id']]['step_count']>0:
                    self.journal.close_decision_interval(context['interval_id'],latest)
            else:
                # Any superseding applied plan is conservative interruption; do not attribute two policies to a route.
                pass
        self._drain_intervals();self._update(runtime,pid,next_choices)
        if not valid:return baseline
        selected=baseline
        # Same-family refinement only: route alternatives stay on the baseline goal/arrival,
        # local alternatives keep the itinerary untouched. No cross-family or invented target.
        if len(valid)>1:
            choices=tuple(LegalChoice(e.action,e.features,next((c.estimated_cost_seconds for c in runtime.decision_candidates[pid]
                if c.candidate_id==p.candidate_id),None)) for p,e in valid)
            if learner.updates>0 and learner.accepted>=learner._parameters['minimum_samples']:
                key=learner.choose(valid[0][1].state,choices,token=runtime.snapshot_id)
                selected=next(p for p,e in valid if e.action==key)
                self.selection_count+=1
                self.selection_by_person[pid]=self.selection_by_person.get(pid,0)+1
                self._emit('selection',person_id=pid,snapshot_id=runtime.snapshot_id,
                    time_seconds=runtime.time_seconds,
                    baseline=asdict(baseline),selected=asdict(selected),
                    scores=[dict(action=list(e.action),score=learner.score(e.state,choices)[e.action]) for p,e in valid],
                    policy='baseline_family_preserved' if mode==_BASELINE_MODE else mode)
        return selected

    def _score_valid(self,runtime,pid,learner,valid):
        warmed=learner.updates>0 and learner.accepted>=learner._parameters['minimum_samples']
        choices=tuple(LegalChoice(e.action,e.features,next((c.estimated_cost_seconds for c in runtime.decision_candidates[pid]
            if c.candidate_id==p.candidate_id),None)) for p,e in valid)
        scores=learner.score(valid[0][1].state,choices) if warmed else {}
        result={}
        for plan,encoded in valid:
            if plan.candidate_id is None:continue
            raw=scores.get(encoded.action)
            if raw is not None and (type(raw) not in (int,float) or not math.isfinite(raw)):
                raw=None
            result[plan.candidate_id]={'action':list(encoded.action),'score':raw}
        return result

    def candidate_scores(self,runtime,baseline):
        """Legal-candidate learner scores exposed as decision/LLM candidate-scoring input.

        Returns ``{candidate_id: {'action': list, 'score': float|None}}``. Before
        warm-up the score is ``None``; the decision owner remains responsible for the
        final plan. No candidate is invented and executor validation still applies.
        """
        self._check(runtime);pid=baseline.person_id
        learner=self._person(runtime,pid)
        if learner is None:return {}
        valid=self._plans(runtime,baseline)
        if not valid:return {}
        return self._score_valid(runtime,pid,learner,valid)

    def boundary_scores(self,runtime,pid):
        """Advisory scores for the current frozen boundary, before a baseline plan exists.

        Only a previously applied route/goal plan seeds the legal candidate set; local
        or absent baselines yield an empty advisory map. Scores never bind the LLM to a
        candidate identity — that contract is enforced at parse time.
        """
        self._check(runtime)
        learner=self._person(runtime,pid)
        if learner is None:return {}
        baseline=runtime.population.states[pid].current_plan
        if baseline is None or baseline.proposed_action not in _ROUTE_FAMILY:
            return {}
        valid=self._plans(runtime,baseline)
        if not valid:return {}
        return self._score_valid(runtime,pid,learner,valid)

    def prepare(self,runtime,plan):
        self._check(runtime);pid=plan.person_id;learner=self._person(runtime,pid)
        if learner is None:return None
        if len(self.ready)>self.capacity-8:raise BufferError('insufficient learning audit capacity before execution')
        _,mode=self._role_mode(runtime,pid)
        if plan.scheduling_source not in self._tracked_sources(mode):
            self.interrupt(runtime,'external_control',person_ids=[pid])
            self._emit('untracked_plan',person_id=pid,reason='nonconfigured_decision_source')
            return None
        if runtime._post_action_constraints(pid).get('control_limits'):
            self.interrupt(runtime,'external_control',person_ids=[pid])
            self._emit('untracked_plan',person_id=pid,reason='external_speed_control')
            return None
        role=learner.binding.identity.role_id;settings=self.roles[role]['setting']
        if settings['interval_mode']=='completed_route' and plan.proposed_action not in {'reroute','change_goal'}:
            return dict(plan=plan,monitor_only=True)
        try:
            encoded=self._encode(runtime,plan,runtime.decision_candidates.get(pid,()))
            learner.binding.validate_target(encoded.action)
        except ValueError as exc:
            self._emit('untracked_plan',person_id=pid,snapshot_id=runtime.snapshot_id,reason=str(exc))
            return None
        if self.journal.active_count>=self.journal.capacity:raise BufferError('learning interval capacity before execution')
        return dict(encoded=encoded,plan=plan)

    def accept(self,runtime,prepared,receipt):
        if prepared is None:return
        self._check(runtime);plan=prepared['plan'];pid=plan.person_id
        if receipt.status!='applied':
            self._emit('execution_not_applied',person_id=pid,receipt=asdict(receipt));return
        if prepared.get('monitor_only'):
            if plan.proposed_action!='continue':
                self.interrupt(runtime,'plan_superseded',person_ids=[pid])
            return
        old=self.contexts.get(pid)
        if old:
            self.journal.interrupt(old['interval_id'],reason='plan_superseded',now=runtime.time_seconds)
            self._drain_intervals()
            # Preserve interrupted evidence, never assign its reward to the new plan.
            self._emit('sample_unavailable',person_id=pid,interval_id=old['interval_id'],reason='plan_superseded')
            self.contexts.pop(pid,None)
        self.interval_sequence+=1
        identity=f'{self.run_id}:learn-interval:{self.interval_sequence}:{pid}'
        mode=self.roles[runtime.population.profile_for(pid).crowd_role]['setting']['interval_mode']
        self.journal.begin(identity,self._point(runtime,pid),plan,receipt,
            runtime.decision_candidates.get(pid,()),mode=mode)
        self.contexts[pid]=dict(interval_id=identity,encoded=prepared['encoded'])

    def advance(self,runtime,*,timeline_end):
        self._check(runtime)
        for pid,context in tuple(self.contexts.items()):
            identity=context['interval_id']
            if identity not in self.journal.pending:continue
            ledger=runtime.population.ledger
            if pid not in runtime.current.persons:
                reason=('unknown_disappearance' if pid in ledger.unknown_disappearances else
                    'explicit_removal' if pid in ledger.explicitly_removed else
                    'sumo_arrived_without_stage' if pid in ledger.arrived_ids else 'missing_observation')
                self.journal.interrupt(identity,reason=reason,now=runtime.time_seconds);continue
            current=self._point(runtime,pid);done=None
            if current.as_dict()['stage_identity']!=self.journal.pending[identity]['route_stage_identity']:
                if self.journal.pending[identity]['mode']=='completed_route':
                    from traci.exceptions import TraCIException
                    try:done=StageEvidence.capture(runtime.adapter.previous_person_stage(pid))
                    except (ValueError, TraCIException):done=None
            self.journal.advance(identity,current,timeline_end=timeline_end,completed_stage=done)
        self._drain_intervals()
        for pid,context in tuple(self.contexts.items()):
            if 'finished' not in context:continue
            if context['finished'].as_dict()['end_reason']!='route_stage_completed':
                self._update(runtime,pid,());continue
            # Completed-route terminal: legal next choices come only from supplied
            # goal_route successor candidates. A missing legal successor leaves the
            # sample unavailable; no continue/local arm is fabricated as a route sample.
            next_choices=self._completed_route_successors(runtime,pid)
            self._update(runtime,pid,next_choices)
        if timeline_end:
            for pid in tuple(self.contexts):self._update(runtime,pid,())

    def interrupt(self,runtime,reason,*,person_ids=None):
        if self.run_id!=runtime.run_id:return
        for pid,context in tuple(self.contexts.items()):
            if person_ids is not None and pid not in person_ids:continue
            self.journal.interrupt(context['interval_id'],reason=reason,now=runtime.time_seconds)
        self._drain_intervals()
        for pid in tuple(self.contexts):
            if person_ids is None or pid in person_ids:self._update(runtime,pid,())

    def close(self,runtime):
        if self.closed:return
        self.interrupt(runtime,'runtime_error' if runtime.last_error else 'runtime_closed')
        self.closed=True

    def batch(self):return tuple(self.ready)

    def acknowledge(self,batch):
        batch=tuple(batch)
        if tuple(self.ready)[:len(batch)]!=batch:raise ValueError('learning audit acknowledgement order mismatch')
        for _ in batch:self.ready.popleft()

    def diagnostics(self):
        return dict(version=VERSION,config_fingerprint=self.fingerprint,run_id=self.run_id,
            runtime_interface_attached=self.run_id is not None,product_contract_ready=self.product_contract_ready,closed=self.closed,
            accepted_samples=self.accepted_samples,actual_updates=self.actual_updates,selections=self.selection_count,
            unpersisted=len(self.ready),roles={r:dict(method_id=d['setting']['learning_config']['method_id'],
                interval_mode=d['setting']['interval_mode']) for r,d in self.roles.items()},
            individuals={pid:{**l.diagnostics(),"selections":self.selection_by_person.get(pid,0)}
                         for pid,l in self.learners.items()},
            intervals=self.journal.diagnostics() if self.journal else None)
