"""Read-only projection of confirmed configuration and already produced state.
No learner allocation, PAD evaluation, planning, SUMO access or learning updates.
"""
from copy import deepcopy
from dataclasses import asdict
import math

VERSION = 'agent-runtime-view-v1'


def _boundary(snapshot, time, runtime):
    return (isinstance(snapshot, str) and snapshot.startswith(runtime.run_id + ':')
            and type(time) in (int, float) and math.isfinite(time)
            and 0 <= time <= runtime.time_seconds)


def build_agent_runtime_view(runtime, profile, state):
    engine = runtime.decision_engine
    contract = getattr(engine, 'execution_contract', None)
    if contract is None:
        return None
    pid = profile.person_id
    if state.person_id != pid:
        raise ValueError('agent display state/person mismatch')
    config = getattr(engine, 'role_configs', {}).get(profile.crowd_role)
    assembly = getattr(engine, 'assemblies', {}).get(profile.crowd_role)
    if config is None or assembly is None:
        return None
    out = dict(version=VERSION, run_id=runtime.run_id, snapshot_id=runtime.snapshot_id,
               time_seconds=runtime.time_seconds, person_id=pid, role_id=profile.crowd_role,
               execution_contract=contract,
               model_id=getattr(runtime, 'artificial_society_id', None),
               model_fingerprint=(getattr(runtime, 'artificial_society_record', None) or {}).get('fingerprint'),
               configuration=dict(capabilities=dict(config.capabilities),
                                  architecture=[dict(id=k, method=config.methods[k]) for k in assembly.order],
                                  parameters=deepcopy(assembly.parameters)),
               goal=dict(current_goal=state.current_goal, pending_goal=state.pending_goal,
                         activity_state=state.activity_state, locked=pid in runtime.population.locked_itinerary_ids),
               affect=dict(status='disabled' if not config.capabilities.get('emotion') else 'not_evaluated',
                           participates_in_decision=assembly.emotion_participates, evaluated_at=None,
                           pad=None, desire=None),
               goal_preparation=None, current_plan=None, last_execution=None,
               learning=dict(status='disabled' if not config.capabilities.get('learning') else 'not_attached'))
    cached = getattr(engine, '_boundary_contexts', {}).get(pid)
    if cached:
        key, context = cached
        _, desire, frozen_profile, frozen_state, observation = context
        if (frozen_profile.person_id == pid and frozen_profile.crowd_role == profile.crowd_role
                and frozen_state.person_id == pid and observation.person_id == pid
                and (observation.snapshot_id, observation.time_seconds) == key
                and _boundary(*key, runtime)):
            measured = engine.emotional_states.get(pid)
            if config.capabilities.get('emotion') and measured is not None:
                out['affect'].update(status='evaluated', evaluated_at=dict(snapshot_id=key[0], time_seconds=key[1]),
                                     pad=measured.as_dict(),
                                     desire=desire.as_dict() if assembly.desire is not None else None)
    trace = engine.goal_trace(pid)
    if trace and _boundary(trace['snapshot_id'], trace['time_seconds'], runtime):
        out['goal_preparation'] = deepcopy(trace)
    plan = state.current_plan
    if (plan is not None and plan.person_id == pid
            and _boundary(plan.snapshot_id, plan.decided_at, runtime)):
        out['current_plan'] = asdict(plan)
    evidence = getattr(getattr(runtime, 'plan_executor', None), 'execution_evidence', {}).get(pid)
    if evidence:
        p, receipt = evidence['plan'], evidence['receipt']
        if (p['person_id'] == receipt['person_id'] == pid
                and _boundary(evidence['snapshot_id'], receipt['applied_at'], runtime)
                and evidence['time_seconds'] == receipt['applied_at']
                and p['snapshot_id'] == evidence['snapshot_id']):
            out['last_execution'] = deepcopy(evidence)
    controller = getattr(runtime, 'learning_controller', None)
    if config.capabilities.get('learning') and controller is not None and controller.run_id == runtime.run_id:
        role = controller.roles.get(profile.crowd_role)
        if role is not None:
            setting = role['setting']['learning_config']
            view = dict(status='not_instantiated', method_id=setting['method_id'],
                        config_fingerprint=controller.fingerprint, experience_scope=setting['experience_scope'],
                        policy_scope=setting['policy_scope'], lifecycle=deepcopy(setting['lifecycle']),
                        accepted_samples=0, update_version=0, binding_fingerprint=None, frozen=None)
            learner = controller.learners.get(pid)
            if learner is not None:
                identity = learner.binding.identity
                if (identity.run_id == runtime.run_id and identity.role_id == profile.crowd_role
                        and identity.subject_id == pid
                        and identity.model_fingerprint == controller.model_fingerprint):
                    view.update(status='instantiated', **learner.diagnostics())
                else:
                    view.update(status='binding_mismatch', accepted_samples=None, update_version=None)
            out['learning'] = view
    trace_store = getattr(runtime, 'decision_traces', None)
    if trace_store is not None:
        latest = trace_store.latest(run_id=runtime.run_id, person_id=pid)
        if (latest is not None and _boundary(latest.get('snapshot_id'), latest.get('time_seconds'), runtime)):
            plan = latest.get('plan') or {}
            receipt = latest.get('receipt') or {}
            learning = latest.get('learning') or {}
            last_update = learning.get('last_update') or {}
            out['decision_trace'] = {
                'version': latest.get('version'),
                'snapshot_id': latest.get('snapshot_id'),
                'time_seconds': latest.get('time_seconds'),
                'action': plan.get('proposed_action'),
                'target_id': plan.get('target_id'),
                'candidate_id': plan.get('candidate_id'),
                'receipt_status': receipt.get('status'),
                'participation': [entry.get('state') for entry in latest.get('participation') or []],
                'learning_acceptance': last_update.get('acceptance'),
            }
    return out
