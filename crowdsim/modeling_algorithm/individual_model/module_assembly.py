"""Whitelisted role-module assembly; physical hooks remain owned by Runtime."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any

from crowdsim.modeling_algorithm.registry import algorithm_catalog
from crowdsim.modeling_algorithm.individual_model.learning.configuration import LEARNING_CONTRACT, validate_learning_config
from crowdsim.modeling_algorithm.individual_model.emotion.pad_continuous import PADContinuousEmotionModel
from crowdsim.modeling_algorithm.individual_model.desire_goal.emotion_driven_desire import EmotionDrivenDesireModel
from crowdsim.modeling_algorithm.individual_model.game.bounded_rationality import BoundedRationalGameModel
from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel

from crowdsim.modeling_algorithm.individual_model.learning.configured_runtime import CONFIGURED_LEARNING_CONTRACT, validate_configured_roles
CONTRACT = 'module-assembly-v1'
CONTRACTS = (CONTRACT, 'desire-goal-v1', 'candidate-decision-v1', CONFIGURED_LEARNING_CONTRACT)
ORDER = ('perception', 'memory', 'desire', 'goal', 'game', 'decision', 'action', 'learning')
# These are existing physical/runtime implementations, not newly implemented algorithms.
NATIVE_HOOKS = {'perception': 'runtime_observation', 'memory': 'runtime_state',
                'goal': 'runtime_goal_route', 'action': 'runtime_plan_executor_sumo'}
PARAMETERS = {'emotion': {'inertia': (0.0, .95)}, 'game': {'rationality': (.1, 100.0)}}


def validate_assembly(agent_types: list[dict[str, Any]], contract: str | None) -> None:
    if isinstance(agent_types, list) and any(isinstance(a, dict) and a.get('learning_draft_dirty') for a in agent_types):
        raise ValueError('role learning input edits not saved')
    if contract == LEARNING_CONTRACT:
        if not isinstance(agent_types, list) or any(not isinstance(a, dict) for a in agent_types):
            raise ValueError('invalid heterogeneous learning role list')
        for agent in agent_types:
            if agent.get('capabilities', {}).get('learning') and 'learning_config' not in agent:
                raise ValueError('incomplete learning configuration: learning_config')
            if 'learning_config' in agent or agent.get('capabilities', {}).get('learning'):
                validate_learning_config(agent.get('learning_config'), complete=bool(agent.get('capabilities', {}).get('learning')))
        raise ValueError('heterogeneous learning runtime is not implemented')
    if contract == CONFIGURED_LEARNING_CONTRACT:
        validate_configured_roles(agent_types)
    if contract != CONFIGURED_LEARNING_CONTRACT and isinstance(agent_types, list) and any(isinstance(a, dict) and ('learning_config' in a or 'learning_runtime' in a) for a in agent_types):
        raise ValueError('learning configuration requires heterogeneous-learning execution contract')
    if contract is None:
        from crowdsim.modeling_algorithm.individual_model.learning.configuration import METHODS
        if any(item.get('method') in {m['label'] for m in METHODS.values()} for agent in agent_types for item in agent.get('architecture',[])):
            raise ValueError('configured learning method requires v2 execution contract')
        if any('algorithm_parameters' in agent for agent in agent_types):
            raise ValueError('algorithm parameters require module-assembly execution contract')
        return
    if contract not in CONTRACTS:
        raise ValueError('unsupported individual execution contract')
    allowed = {(a['module'], a['method']) for a in algorithm_catalog()
               if a['model'] == 'individual' and a['implemented']
               and (not a.get('execution_contract') or a['execution_contract'] == contract)}
    if not isinstance(agent_types, list) or not agent_types or any(not isinstance(agent, dict) for agent in agent_types):
        raise ValueError('module assembly requires a nonempty role list')
    role_ids = set()
    for agent in agent_types:
        role = agent.get('id')
        if not isinstance(role, str) or not role or role in role_ids:
            raise ValueError('module assembly requires unique role ids')
        role_ids.add(role)
        capabilities = agent.get('capabilities') or {}
        if any(type(capabilities.get(key)) is not bool for key in ('emotion', 'game', 'learning')):
            raise ValueError('module assembly capabilities must be booleans')
        architecture = agent.get('architecture')
        if not isinstance(architecture, list) or any(not isinstance(item, dict) for item in architecture):
            raise ValueError('module assembly architecture must be a list of objects')
        selected = [item for item in architecture if item.get('selected') is not False]
        ids = [item.get('id') for item in selected]
        if len(ids) != len(set(ids)) or ids != [key for key in ORDER if key in ids]:
            raise ValueError('module assembly architecture order is invalid')
        if not set(NATIVE_HOOKS).issubset(ids) or 'decision' not in ids:
            raise ValueError('module assembly missing required runtime hooks')
        if any((item.get('id'), item.get('method')) not in allowed for item in selected):
            raise ValueError('module assembly method is not executable')
        emotion = agent.get('emotionState') or {}
        if capabilities['emotion']:
            if (('emotion', emotion.get('method')) not in allowed
                    or type(emotion.get('participates_in_operation')) is not bool):
                raise ValueError('module assembly emotion contract is invalid')
        elif emotion.get('participates_in_operation'):
            raise ValueError('disabled emotion cannot participate in operation')
        for module, expected in [('desire', capabilities['emotion'] and emotion.get('participates_in_operation')),
                                 ('game', capabilities['game']), ('learning', capabilities['learning'])]:
            if (module in ids) != bool(expected):
                raise ValueError(f'module assembly capability differs from {module}')
        parameters = agent.get('algorithm_parameters', {})
        if not isinstance(parameters, dict) or set(parameters) - set(PARAMETERS):
            raise ValueError('unsupported algorithm parameter module')
        for module, values in parameters.items():
            if not isinstance(values, dict) or set(values) - set(PARAMETERS[module]):
                raise ValueError('unsupported algorithm parameter')
            if not capabilities.get(module):
                raise ValueError('parameters for a disabled module are not allowed')
            for name, value in values.items():
                lo, hi = PARAMETERS[module][name]
                if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
                    raise ValueError(f'invalid algorithm parameter: {module}.{name}')


@dataclass
class RoleAssembly:
    role_id: str
    order: tuple[str, ...]
    emotion_participates: bool
    emotion: PADContinuousEmotionModel | None
    desire: EmotionDrivenDesireModel | None
    game: BoundedRationalGameModel | None
    decision: LLMDrivenDecisionModel | None
    parameters: dict[str, Any]
    learning_requested: bool
    contract: str = CONTRACT
    learning_settings: dict[str, Any] | None = None

    def diagnostics(self):
        return {'order': list(self.order), 'native_hooks': dict(NATIVE_HOOKS),
                'emotion_participates': self.emotion_participates,
                'parameters': deepcopy(self.parameters),
                'learning_status': ('configured_post_action' if self.learning_settings else 'awaiting_learning_policy') if self.learning_requested else 'disabled',
                'goal_status': ('desire_before_reachable_routes' if self.desire else 'existing_runtime_hook; no_desire_module') if self.contract in {'desire-goal-v1', 'candidate-decision-v1', CONFIGURED_LEARNING_CONTRACT} else 'existing_runtime_hook; desire_to_goal_pending',
                'action_status': 'existing_executor_and_sumo_hook'}


def assemble_roles(agent_types, contract):
    validate_assembly(agent_types, contract)
    if contract is None:
        return {}
    roles = {}
    learning_settings = validate_configured_roles(agent_types)[0] if contract == CONFIGURED_LEARNING_CONTRACT else {}
    for agent in deepcopy(agent_types):
        caps = agent['capabilities']
        params = agent.get('algorithm_parameters') or {}
        methods = {item['id']: item['method'] for item in agent['architecture'] if item.get('selected') is not False}
        roles[agent['id']] = RoleAssembly(
            role_id=agent['id'], order=tuple(methods),
            emotion_participates=bool(caps['emotion'] and agent.get('emotionState', {}).get('participates_in_operation')),
            emotion=PADContinuousEmotionModel(**params.get('emotion', {})) if caps['emotion'] else None,
            desire=EmotionDrivenDesireModel() if 'desire' in methods else None,
            game=BoundedRationalGameModel(**params.get('game', {})) if caps['game'] else None,
            decision=LLMDrivenDecisionModel() if methods.get('decision') == 'LLM-driven' else None,
            parameters=params, learning_requested=caps['learning'], contract=contract,
            learning_settings=deepcopy(learning_settings.get(agent['id'])),
        )
        if roles[agent['id']].decision is not None:
            roles[agent['id']].decision.candidate_contract = contract in {'candidate-decision-v1', CONFIGURED_LEARNING_CONTRACT}
    return roles
