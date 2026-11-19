"""Immutable post-action evidence. No rewards, sampling or policy updates."""
from collections import Counter, deque
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import math

VERSION = 'post-action-evidence-v1'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False, default=lambda v: asdict(v) if is_dataclass(v) else _unsupported(v))


def _unsupported(value):
    raise TypeError(f'unsupported evidence value: {type(value).__name__}')


@dataclass(frozen=True)
class LearningTransition:
    transition_id: str
    payload_json: str

    def as_dict(self):
        # Return detached JSON; no mutable state is held by this contract.
        return json.loads(self.payload_json)


class PostActionJournal:
    def __init__(self, run_id, *, capacity=4096):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('evidence capacity must be a positive integer')
        self.run_id = run_id
        self.capacity = capacity
        self.pending = {}
        self.ready = deque()
        self.recent = deque(maxlen=capacity)
        self._seen = set()
        self._seen_order = deque()
        self.counts = Counter()
        self.sequence = 0

    def prepare(self, plan, profile, state, observation, candidates, *, snapshot_id, now,
                model_id=None, model_fingerprint=None, contract=None, constraints=None, affect=None):
        if profile.person_id != plan.person_id or state.person_id != plan.person_id:
            raise ValueError('post-action person mismatch')
        if observation is not None and (observation.person_id != plan.person_id or observation.snapshot_id != snapshot_id):
            raise ValueError('post-action snapshot mismatch')
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith(self.run_id + ':'):
            raise ValueError('post-action evidence belongs to another run')
        if not math.isfinite(now) or observation is not None and (observation.time_seconds != now or observation.own_motion.time_seconds != now or observation.own_motion.person_id != plan.person_id):
            raise ValueError('post-action observation time mismatch')
        if len(self.pending) + len(self.ready) >= self.capacity:
            raise BufferError('post-action evidence capacity exhausted before execution')
        raw = dict(version=VERSION, run_id=self.run_id, model_id=model_id,
                   model_fingerprint=model_fingerprint, execution_contract=contract,
                   person_id=plan.person_id, role_id=profile.crowd_role,
                   before_snapshot_id=snapshot_id, started_at=now,
                   before=dict(profile=profile, state=state, observation=observation,
                               affect=affect or {}, constraints=constraints or {}),
                   plan=plan, candidates=list(candidates),
                   interval_semantics='next_physical_step', reward=None, reward_status='not_evaluated',
                   learning_update=None, learning_status='evidence_only')
        payload = encode(raw)
        key = hashlib.sha256(payload.encode()).hexdigest()
        return key, payload

    def accept_receipt(self, prepared, receipt):
        key, payload = prepared
        before = json.loads(payload)
        if before['run_id'] != self.run_id:
            raise ValueError('old run receipt')
        if receipt.person_id != before['person_id'] or receipt.proposed_action != before['plan']['proposed_action']:
            raise ValueError('execution receipt mismatch')
        if receipt.applied_at != before['started_at'] or receipt.status not in ('applied','rejected','deferred','partial_failure'):
            raise ValueError('execution receipt time or status mismatch')
        if key in self._seen:
            self.counts['duplicates'] += 1
            return
        if len(self.pending) + len(self.ready) >= self.capacity:
            raise BufferError('post-action evidence capacity exhausted')
        self.sequence += 1
        transition_id = f'{self.run_id}:transition:{self.sequence}'
        before.update(transition_id=transition_id, execution=asdict(receipt))
        if receipt.status == 'applied':
            for identity, raw in tuple(self.pending.items()):
                old = json.loads(raw)
                if old['person_id'] == before['person_id']:
                    self._finish(old, None, None, before['started_at'], None,
                                 end_reason='plan_superseded', evidence_status='interrupted', truncated=True)
                    del self.pending[identity]
        self._seen.add(key); self._seen_order.append(key)
        while len(self._seen_order) > self.capacity:
            self._seen.discard(self._seen_order.popleft())
        self.counts['receipts'] += 1
        if receipt.status != 'applied':
            self._finish(before, None, None, before['started_at'], None,
                         end_reason=receipt.status, evidence_status='execution_not_applied')
        else:
            self.pending[transition_id] = encode(before)

    def _finish(self, before, observation, state, now, snapshot_id, *, end_reason,
                evidence_status, terminated=False, truncated=False, constraints=None):
        dt = now - before['started_at']
        if not math.isfinite(dt) or dt < 0:
            raise ValueError('post-action time moved backwards')
        after = dict(observation=observation, state=state, constraints=constraints or {}) if observation is not None else None
        before.update(after=after, after_snapshot_id=snapshot_id, ended_at=now,
                      duration_seconds=dt, terminated=terminated, truncated=truncated,
                      end_reason=end_reason, evidence_status=evidence_status,
                      trainable=False)  # A later confirmed reward/learner must explicitly opt in.
        if observation is not None:
            c = constraints or {}
            state_wait = getattr(state, 'planned_wait_until', None)
            before['observed_activity'] = ('flow_hold' if c.get('flow_hold') else
                'activity_hold' if c.get('activity_hold') else
                'policy_wait' if c.get('policy_wait') or state_wait is not None and state_wait > now else
                'waiting_stage' if observation.own_motion.stage_type == 1 or getattr(state, 'activity_state', None) in ('waiting','activity','hotspot_dwelling') else
                'low_speed_unclassified' if observation.own_motion.speed <= c.get('low_speed_threshold', .2) else 'moving')
            before['observed_facts'] = dict(speed=observation.own_motion.speed,
                perceived_risk=observation.perceived_risk,
                perceived_crowding=observation.perceived_crowding,
                objective_density_per_m2=observation.objective_density_per_m2,
                blocked_duration=getattr(state, 'blocked_duration', None))
        item = LearningTransition(before['transition_id'], encode(before))
        self.ready.append(item); self.recent.append(item)
        self.counts['completed'] += 1
        self.counts[evidence_status] += 1

    def advance(self, *, snapshot_id, now, observations, states, ledger,
                timeline_end=False, constraints=None):
        if not snapshot_id.startswith(self.run_id + ':'):
            raise ValueError('post-action next snapshot belongs to another run')
        constraints = constraints or {}
        for identity, raw in tuple(self.pending.items()):
            before = json.loads(raw); pid = before['person_id']
            if now < before['started_at']:
                raise ValueError('post-action time moved backwards')
            if now == before['started_at']:
                continue
            observation = observations.get(pid)
            state = states.get(pid)
            if pid in ledger.unknown_disappearances:
                args = dict(end_reason='unknown_disappearance', evidence_status='missing_post_observation', truncated=True)
                observation = None
            elif pid in ledger.explicitly_removed:
                args = dict(end_reason='explicit_removal', evidence_status='externally_removed', truncated=True)
                observation = None
            elif pid in ledger.arrived_ids:
                args = dict(end_reason='sumo_arrived', evidence_status='terminal_without_post_observation', terminated=True)
                observation = None
            elif observation is not None and state is not None:
                if observation.snapshot_id != snapshot_id or observation.time_seconds != now or observation.own_motion.time_seconds != now or observation.person_id != pid or observation.own_motion.person_id != pid or state.person_id != pid:
                    raise ValueError('post-action after observation mismatch')
                before_controls = before['before']['constraints'].get('control_limits', {})
                after_controls = constraints.get(pid, {}).get('control_limits', {})
                evidence_status = 'external_control_observed' if before_controls or after_controls else 'observed'
                args = dict(end_reason='timeline_end' if timeline_end else 'next_physical_step',
                            evidence_status=evidence_status, truncated=timeline_end)
            else:
                args = dict(end_reason='missing_post_observation', evidence_status='missing_post_observation', truncated=True)
                observation = None
            self._finish(before, observation, state, now, snapshot_id, constraints=constraints.get(pid), **args)
            del self.pending[identity]

    def interrupt(self, now, reason, *, person_ids=None):
        people = set(person_ids) if person_ids is not None else None
        for identity, raw in tuple(self.pending.items()):
            before = json.loads(raw)
            if people is not None and before['person_id'] not in people:
                continue
            self._finish(before, None, None, now, None,
                         end_reason=reason, evidence_status='interrupted', truncated=True)
            del self.pending[identity]

    def batch(self):
        return tuple(self.ready)

    def acknowledge(self, batch):
        for item in batch:
            if not self.ready or self.ready[0] != item:
                raise ValueError('post-action acknowledgement order mismatch')
            self.ready.popleft()

    def diagnostics(self):
        return dict(version=VERSION, interval_semantics='next_physical_step',
                    pending=len(self.pending), unpersisted=len(self.ready),
                    capacity=self.capacity, **dict(self.counts), policy_updates=0)
