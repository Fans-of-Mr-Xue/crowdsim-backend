"""Versioned confirmed-configuration binding, not an algorithm-completion claim."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

VERSION = 'confirmed-configuration-v1'
IMPLEMENTATION_VERSION = 'legacy-pipeline-v1'


def build_runtime_binding(individual: dict[str, Any]) -> dict[str, Any]:
    roles = []
    for agent in individual.get('agent_types') or []:
        roles.append({
            'role_id': str(agent['id']),
            'capabilities': deepcopy(agent.get('capabilities') or {}),
            'emotion_state': deepcopy(agent.get('emotionState') or {}),
            'architecture': [
                {'id': str(item['id']), 'method': str(item['method'])}
                for item in agent.get('architecture') or [] if item.get('selected') is not False
            ],
            'profile_parameters': deepcopy(agent.get('attributes') or {}),
            **({'algorithm_parameters': deepcopy(agent['algorithm_parameters'])} if 'algorithm_parameters' in agent else {}),
            **({'learning_config': deepcopy(agent['learning_config'])} if 'learning_config' in agent else {}),
            **({'learning_runtime': deepcopy(agent['learning_runtime'])} if 'learning_runtime' in agent else {}),
        })
    configuration = {
        'roles': sorted(roles, key=lambda row: row['role_id']),
        'profile_parameters': deepcopy(individual.get('profile_parameters') or {}),
        **({'execution_contract': individual['execution_contract']} if 'execution_contract' in individual else {}),
    }
    canonical = json.dumps(configuration, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
    return {
        'version': VERSION,
        'implementation_version': individual.get('execution_contract') or IMPLEMENTATION_VERSION,
        'semantics': ('configured_post_action_learning; baseline_route_refinement'
                      if individual.get('execution_contract') == 'heterogeneous-learning-v2' else
                      'role_learning_configuration; runtime_not_ready'
                      if individual.get('execution_contract') == 'heterogeneous-learning-v1' else
                      'unique_candidates; eligible_llm_budget; desire_to_goal; learning_policy_pending'
                      if individual.get('execution_contract') == 'candidate-decision-v1' else
                      'desire_to_goal; reachable_routes; learning_policy_pending'
                      if individual.get('execution_contract') == 'desire-goal-v1' else
                      'selected_modules; learning_policy_pending' if individual.get('execution_contract') == 'module-assembly-v1' else 'configuration_only'),
        **configuration,
        'configuration_fingerprint': f'sha256:{hashlib.sha256(canonical.encode()).hexdigest()}',
    }


def validate_runtime_binding(payload: dict[str, Any]) -> dict[str, Any] | None:
    # Absence preserves historical model content and fingerprints. Explicit null
    # or an unsupported version is never silently treated as legacy.
    if 'runtime_binding' not in payload:
        return None
    supplied = payload['runtime_binding']
    if not isinstance(supplied, dict) or supplied.get('version') != VERSION:
        raise ValueError('runtime binding version is unsupported')
    expected = build_runtime_binding(payload['individual_model'])
    if supplied != expected:
        raise ValueError('runtime binding does not match confirmed configuration')
    return expected
