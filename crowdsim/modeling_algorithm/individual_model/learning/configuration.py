"""Draft schemas for role-specific learners; no runtime readiness inferred from config."""
from copy import deepcopy
import math

LEARNING_CONTRACT = 'heterogeneous-learning-v1'
COMMON_PARAMETERS = {
    'minimum_samples': (1, 100000, 'integer'),
    'update_every': (1, 10000, 'integer'),
    'update_budget': (1, 1000, 'integer'),
}
METHODS = {
    'bounded_adaptation_v1': {
        'label': '有界参数自适应', 'implementation_version': '1',
        'parameters': {**COMMON_PARAMETERS, 'learning_rate': (0.000001, 1, 'number'),
                       'forgetting_factor': (0, 1, 'number'), 'correction_limit': (0.000001, 10000, 'number')},
    },
    'linucb_v1': {
        'label': '上下文 bandit（LinUCB）', 'implementation_version': '1',
        'parameters': {**COMMON_PARAMETERS, 'exploration_coefficient': (0, 100, 'number'),
                       'regularization': (0.000001, 10000, 'number')},
    },
    'tabular_q_v1': {
        'label': '表格 Q-learning', 'implementation_version': '1',
        'parameters': {**COMMON_PARAMETERS, 'learning_rate': (0.000001, 1, 'number'),
                       'discount_factor': (0, 1, 'number'), 'epsilon': (0, 1, 'number'),
                       'table_capacity': (1, 100000, 'integer')},
    },
}
KEYS = {'method_id', 'implementation_version', 'targets', 'feature_schema_version',
        'action_schema_version', 'parameters', 'experience_scope', 'policy_scope',
        'lifecycle', 'feedback', 'replay'}
REQUIRED = KEYS - {'replay'}
FEEDBACK_KEYS = {'rule_id', 'version', 'weights'}
WEIGHTS = {'goal_progress', 'risk_exposure', 'unplanned_blocking', 'switch_cost'}


def learning_catalog():
    # A separate draft catalogue, never merged into executable algorithm_catalog.
    return [dict(method_id=key, **deepcopy(value), implemented=False,
                 runtime_ready=False, status='planned') for key, value in METHODS.items()]


def _object(value, allowed, name):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f'invalid learning {name}')


def _number(value, lo, hi, kind, name):
    if type(value) not in (int, float) or not lo <= value <= hi or not math.isfinite(value) or (kind == 'integer' and value != int(value)):
        raise ValueError(f'invalid learning parameter: {name}')


def validate_learning_config(config, *, complete=False):
    """Validate a partial editable draft; complete does not imply runtime-ready."""
    _object(config, KEYS, 'configuration')
    method = config.get('method_id')
    if 'method_id' in config and (not isinstance(method, str) or method not in METHODS):
        raise ValueError('unknown learning method')
    if 'implementation_version' in config and config['implementation_version'] != '1':
        raise ValueError('unsupported learning implementation version')
    for key in ('feature_schema_version', 'action_schema_version'):
        if key in config and (not isinstance(config[key], str) or not config[key].strip() or len(config[key]) > 120):
            raise ValueError(f'invalid learning {key}')
    if 'targets' in config:
        values = config['targets']
        if not isinstance(values, list) or not values or any(v not in ('goal_route', 'action') for v in values) or len(values) != len(set(values)):
            raise ValueError('invalid learning targets')
    for key in ('experience_scope', 'policy_scope'):
        if key in config and config[key] not in ('individual', 'role'):
            raise ValueError(f'invalid learning {key}')
    if 'parameters' in config:
        if method is None:
            raise ValueError('learning parameters require a method')
        bounds = METHODS[method]['parameters']
        _object(config['parameters'], set(bounds), 'parameters')
        for name, value in config['parameters'].items():
            _number(value, *bounds[name], name)
    if 'lifecycle' in config:
        life = config['lifecycle']
        _object(life, {'mode', 'checkpoint_ref', 'cohort_key'}, 'lifecycle')
        if 'mode' in life and life['mode'] not in ('reset', 'frozen', 'inherit'):
            raise ValueError('invalid learning lifecycle mode')
        for key in ('checkpoint_ref', 'cohort_key'):
            if key in life and (not isinstance(life[key], str) or not life[key].strip() or len(life[key]) > 240):
                raise ValueError(f'invalid learning {key}')
        if life.get('mode') == 'reset' and set(life) & {'checkpoint_ref', 'cohort_key'}:
            raise ValueError('reset learning cannot load a checkpoint')
    if 'feedback' in config:
        feedback = config['feedback']
        _object(feedback, FEEDBACK_KEYS, 'feedback')
        for key in ('rule_id', 'version'):
            if key in feedback and (not isinstance(feedback[key], str) or not feedback[key].strip() or len(feedback[key]) > 120):
                raise ValueError(f'invalid learning feedback {key}')
        if 'weights' in feedback:
            _object(feedback['weights'], WEIGHTS, 'feedback weights')
            for name, value in feedback['weights'].items():
                _number(value, 0, 100, 'number', name)
    if 'replay' in config:
        if method != 'tabular_q_v1':
            raise ValueError('replay configuration requires tabular Q-learning')
        replay = config['replay']
        _object(replay, {'enabled', 'capacity', 'batch_size'}, 'replay')
        if 'enabled' in replay and type(replay['enabled']) is not bool:
            raise ValueError('invalid replay enabled')
        for key, maximum in [('capacity', 100000), ('batch_size', 1000)]:
            if key in replay:
                _number(replay[key], 1, maximum, 'integer', key)
        if replay.get('enabled') is False and set(replay) & {'capacity', 'batch_size'}:
            raise ValueError('disabled replay cannot have active parameters')
        if 'batch_size' in replay and 'capacity' in replay and replay['batch_size'] > replay['capacity']:
            raise ValueError('replay batch exceeds capacity')
    if complete:
        missing = REQUIRED - set(config)
        if 'replay' in config and 'enabled' not in config['replay']:
            missing.add('replay.enabled')
        if method is not None:
            missing |= {f'parameters.{x}' for x in set(METHODS[method]['parameters']) - set(config.get('parameters', {}))}
        if not config.get('lifecycle', {}).get('mode'):
            missing.add('lifecycle.mode')
        elif config['lifecycle']['mode'] in ('frozen', 'inherit'):
            if not config['lifecycle'].get('checkpoint_ref'):
                missing.add('lifecycle.checkpoint_ref')
            if config['lifecycle']['mode'] == 'inherit' and config.get('policy_scope') == 'individual' and not config['lifecycle'].get('cohort_key'):
                missing.add('lifecycle.cohort_key')
        missing |= {f'feedback.{x}' for x in FEEDBACK_KEYS - set(config.get('feedback', {}))}
        if not any(config.get('feedback', {}).get('weights', {}).values()):
            missing.add('feedback.nonzero_weights')
        if config.get('replay', {}).get('enabled') is True:
            missing |= {f'replay.{x}' for x in {'capacity', 'batch_size'} - set(config['replay'])}
        if missing:
            raise ValueError(f'incomplete learning configuration: {", ".join(sorted(missing))}')
    return deepcopy(config)


def require_learning_ready(config):
    validate_learning_config(config, complete=True)
    # Only server-side executable factories may eventually change this gate.
    raise ValueError('heterogeneous learning runtime is not implemented')
