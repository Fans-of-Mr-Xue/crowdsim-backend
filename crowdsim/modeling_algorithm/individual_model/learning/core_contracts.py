"""Explicit inputs for learner cores; no implicit reward, state encoder or live enablement."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical(value).encode()).hexdigest()


def finite(value, name, *, lo=-1e9, hi=1e9):
    if type(value) not in (int, float) or not lo <= value <= hi or not math.isfinite(value):
        raise ValueError(f'invalid learner {name}')
    return float(value)


def text(value, name, *, limit=240):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f'invalid learner {name}')
    return value


def action_key(key):
    if not isinstance(key, tuple) or not 1 <= len(key) <= 8:
        raise ValueError('learner action must be a stable semantic tuple')
    if not isinstance(key[0], str) or key[0] not in {'continue', 'slow_down', 'wait', 'reroute', 'change_goal'}:
        raise ValueError('unsupported learner action')
    for part in key:
        if not isinstance(part, str) or len(part) > 240 or part.startswith('candidate-'):
            raise ValueError('snapshot candidate identity is not a learning action')
    return key


def discrete_state(state):
    if not isinstance(state, tuple) or not state or len(state) > 16:
        raise ValueError('learner state must be a bounded discrete tuple')
    for part in state:
        if type(part) is int:
            if not -100000 <= part <= 100000:
                raise ValueError('learner state index outside bound')
        elif not isinstance(part, str) or not part or len(part) > 120:
            raise ValueError('invalid learner discrete state')
    return state


def features(vector, size):
    if not isinstance(vector, tuple) or len(vector) != size:
        raise ValueError('learner feature dimension mismatch')
    return tuple(finite(x, 'feature', lo=-1000, hi=1000) for x in vector)


@dataclass(frozen=True)
class LearnerIdentity:
    run_id: str
    model_fingerprint: str
    role_id: str
    subject_id: str

    def __post_init__(self):
        for name, value in asdict(self).items():
            text(value, name)


@dataclass(frozen=True)
class LearningSample:
    sample_id: str
    binding_fingerprint: str
    action: tuple[str, ...]
    state: tuple
    features: tuple[float, ...]
    reward: float
    duration_seconds: float
    next_state: tuple
    next_actions: tuple[tuple[str, ...], ...]
    terminated: bool
    truncated: bool
    feedback_rule_id: str
    feedback_version: str
    evidence_id: str
    # Optional completed-cost evidence, only consumed by bounded adaptation.
    predicted_cost_seconds: float | None = None
    observed_cost_seconds: float | None = None
    cost_complete: bool = False
    # Optional supplied-candidate identity attached to the executed sample.
    target_id: str | None = None
    candidate_id: str | None = None


@dataclass(frozen=True)
class LearningUpdate:
    payload_json: str

    def as_dict(self):
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class LegalChoice:
    key: tuple[str, ...]
    features: tuple[float, ...]
    predicted_cost_seconds: float | None = None


class CoreBinding:
    def __init__(self, config, identity, *, feature_dimension, time_unit_seconds,
                 bootstrap_on_truncation, storage_capacity):
        from .configuration import validate_learning_config
        checked_config = validate_learning_config(config, complete=True)
        self._config_json = canonical(checked_config)
        if not isinstance(identity, LearnerIdentity):
            raise ValueError('explicit learner identity required')
        if type(feature_dimension) is not int or not 1 <= feature_dimension <= 32:
            raise ValueError('invalid learner feature dimension')
        if type(storage_capacity) is not int or not 1 <= storage_capacity <= 100000:
            raise ValueError('invalid learner storage capacity')
        if type(bootstrap_on_truncation) is not bool:
            raise ValueError('explicit truncation bootstrap rule required')
        finite(time_unit_seconds, 'time unit', lo=.000001, hi=1e6)
        # Shared policies and filesystem checkpoint loading are deliberately not implemented.
        if self.config['experience_scope'] != 'individual' or self.config['policy_scope'] != 'individual':
            raise ValueError('core only supports explicit individual scope')
        if self.config['lifecycle']['mode'] != 'reset':
            raise ValueError('core creation requires reset; compatible checkpoint load and freeze are explicit operations')
        if self.config['parameters']['minimum_samples'] > storage_capacity:
            raise ValueError('minimum samples exceeds core storage capacity')
        if self.config.get('replay', {}).get('enabled') and self.config['replay']['capacity'] > storage_capacity:
            raise ValueError('replay capacity exceeds core storage capacity')
        self.identity = identity
        self.dimension = feature_dimension
        self.capacity = storage_capacity
        self.time_unit = time_unit_seconds
        self.bootstrap_on_truncation = bootstrap_on_truncation
        self.fingerprint = digest(dict(config=self.config, identity=asdict(identity),
            feature_dimension=feature_dimension, time_unit_seconds=time_unit_seconds,
            bootstrap_on_truncation=bootstrap_on_truncation, storage_capacity=storage_capacity))

    @property
    def config(self):
        return json.loads(self._config_json)

    def validate(self, sample):
        if not isinstance(sample, LearningSample):
            raise ValueError('explicit evaluated LearningSample required, not raw evidence')
        text(sample.sample_id, 'sample id');text(sample.evidence_id, 'evidence id')
        if sample.binding_fingerprint != self.fingerprint:
            raise ValueError('sample binding mismatch')
        action_key(sample.action);discrete_state(sample.state);discrete_state(sample.next_state)
        self.validate_target(sample.action)
        features(sample.features, self.dimension)
        finite(sample.reward, 'reward', lo=-1e6, hi=1e6)
        finite(sample.duration_seconds, 'duration', lo=.000001, hi=1e6)
        if type(sample.terminated) is not bool or type(sample.truncated) is not bool or sample.terminated and sample.truncated:
            raise ValueError('invalid learner terminal flags')
        if not isinstance(sample.next_actions, tuple):
            raise ValueError('next actions must be an immutable tuple')
        if len(sample.next_actions) > 256:
            raise ValueError('excessive next actions')
        for key in sample.next_actions:
            action_key(key)
            self.validate_target(key)
        if len(set(sample.next_actions)) != len(sample.next_actions):
            raise ValueError('duplicate next actions')
        if not sample.terminated and not sample.next_actions:
            raise ValueError('nonterminal sample requires explicit next legal actions')
        fb = self.config['feedback']
        if sample.feedback_rule_id != fb['rule_id'] or sample.feedback_version != fb['version']:
            raise ValueError('feedback rule/version mismatch')
        if type(sample.cost_complete) is not bool:
            raise ValueError('invalid cost completion flag')
        for value in (sample.predicted_cost_seconds, sample.observed_cost_seconds):
            if value is not None:
                finite(value, 'cost', lo=0, hi=1e9)
        return sample

    def validate_target(self, key):
        target = 'goal_route' if key[0] in {'reroute','change_goal'} else 'action'
        if target not in self.config['targets']:
            raise ValueError('action outside confirmed learning targets')

    def choices(self, choices):
        values = tuple(choices)
        if not values or len(values) > 256 or any(not isinstance(c, LegalChoice) for c in values):
            raise ValueError('explicit nonempty legal choice set required')
        keys = [action_key(c.key) for c in values]
        if len(keys) != len(set(keys)):
            raise ValueError('legal choices need unique stable semantics')
        for c in values:
            self.validate_target(c.key)
            features(c.features, self.dimension)
            if c.predicted_cost_seconds is not None:
                finite(c.predicted_cost_seconds, 'predicted cost', lo=0, hi=1e9)
        return values


def semantic_route_key(action, candidate):
    """Stable itinerary semantics; excludes snapshot ID and dynamic costs/density."""
    if action not in {'reroute', 'change_goal'}:
        raise ValueError('route semantic key requires a route action')
    stable = dict(edges=candidate.edges, target_edge=candidate.target_edge, arrival=candidate.arrival_position,
        kind=candidate.target_kind, activity=candidate.activity_duration if action=='change_goal' else None,
        next_edges=candidate.next_route_edges if action=='change_goal' else (),
        next_arrival=candidate.next_arrival_position if action=='change_goal' else None,
        next_target=candidate.next_target_id if action=='change_goal' else None)
    return action_key((action, candidate.target_id, candidate.entry_edge or '', digest(stable)))


def choose_route_candidate(core, state, candidates, feature_vectors, *, action, token):
    """Explicit test/adapter entry: no invented action, features, roads or BehaviorPlan."""
    from crowdsim.decision.route_provider import select_candidate
    if action not in {'reroute', 'change_goal'}:
        raise ValueError('route adapter needs explicit route action')
    values=tuple(candidates);vectors=tuple(feature_vectors)
    if len(values)!=len(vectors):
        raise ValueError('one feature vector per candidate required')
    choices=[];mapping={}
    for candidate, vector in zip(values, vectors):
        if not candidate.candidate_id:
            raise ValueError('route adapter needs frozen candidate identity')
        key=semantic_route_key(action,candidate)
        if key in mapping:
            raise ValueError('multiple frozen candidates have identical stable semantics')
        mapping[key]=candidate
        choices.append(LegalChoice(key,vector,candidate.estimated_cost_seconds))
    key=core.choose(state,choices,token=token)
    chosen=mapping[key]
    return select_candidate(values,candidate_id=chosen.candidate_id,target_id=chosen.target_id)
