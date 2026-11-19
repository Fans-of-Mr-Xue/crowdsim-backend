"""Confirmed v2 contract for currently implemented per-role learning policy.

Declared definition versions remain in the model. Fingerprints used by internal
learner bindings are deterministically derived without changing confirmed input.
"""
from copy import deepcopy
from .configuration import validate_learning_config, METHODS
from .observed_feedback import FEATURE_VERSION, ACTION_VERSION, FeatureEncoder
from .reward_bridge import RULE_ID, IntervalRewardRule

CONFIGURED_LEARNING_CONTRACT='heterogeneous-learning-v2'
RUNTIME_KEYS={'encoding','reward_scales','interval_mode','integration_mode',
              'storage_capacity','bootstrap_on_truncation','audit_capacity'}


def prepare_role_learning_settings(config,runtime_spec):
    config=validate_learning_config(config,complete=True)
    if not isinstance(runtime_spec,dict) or set(runtime_spec)!=RUNTIME_KEYS:
        raise ValueError('incomplete or unknown confirmed learning runtime specification')
    if config['feature_schema_version']!=FEATURE_VERSION or config['action_schema_version']!=ACTION_VERSION:
        raise ValueError('unsupported confirmed learning feature/action definitions')
    if config['feedback']['rule_id']!=RULE_ID or config['feedback']['version']!='1':
        raise ValueError('unsupported confirmed feedback definition')
    for key,lo in [('storage_capacity',1),('audit_capacity',16)]:
        if type(runtime_spec[key]) is not int or not lo<=runtime_spec[key]<=100000:
            raise ValueError('invalid confirmed learning capacity')
    enc=runtime_spec['encoding']
    if not isinstance(enc,dict) or set(enc)!={'columns','scales','state_bins','emotion_participates'}:
        raise ValueError('incomplete confirmed learning encoding')
    if not isinstance(enc['columns'],list) or not isinstance(enc['state_bins'],dict) or any(not isinstance(v,list) for v in enc['state_bins'].values()):
        raise ValueError('invalid confirmed learning encoding lists')
    encoder=FeatureEncoder(columns=tuple(enc['columns']),scales=enc['scales'],
        state_bins={k:tuple(v) for k,v in enc['state_bins'].items()},emotion_participates=enc['emotion_participates'])
    rule=IntervalRewardRule(weights=config['feedback']['weights'],scales=runtime_spec['reward_scales'])
    if config['parameters'].get('table_capacity',1)>runtime_spec['storage_capacity']:
        raise ValueError('Q table capacity exceeds explicit individual storage capacity')
    effective=deepcopy(config)
    effective['feature_schema_version']=encoder.fingerprint
    effective['feedback']['version']=rule.fingerprint
    settings={k:deepcopy(v) for k,v in runtime_spec.items() if k!='audit_capacity'}
    settings['learning_config']=effective
    from .runtime_controller import RoleLearningController
    RoleLearningController({'validation':settings},capacity=runtime_spec['audit_capacity'])
    return settings


def validate_configured_roles(agents):
    if not isinstance(agents,list) or not agents:raise ValueError('configured learning requires role list')
    settings={};capacities=set()
    for agent in agents:
        if not isinstance(agent,dict):raise ValueError('invalid configured learning role')
        if agent.get('learning_draft_dirty'):
            raise ValueError('role learning input edits not saved')
        coop=(agent.get('learning_runtime') or {}).get('integration_mode')=='llm_baseline_refine'
        if any(m.get('id')=='decision' and m.get('method')=='LLM-driven' and m.get('selected') is not False for m in agent.get('architecture',[])) and not coop:
            raise ValueError('configured learning run currently requires rule decisions for all roles')
        enabled=agent.get('capabilities',{}).get('learning')
        if not enabled:
            if 'learning_config' in agent:validate_learning_config(agent['learning_config'])
            # Disabled drafts can be retained, but never executed or silently completed.
            continue
        role=agent.get('id')
        if not isinstance(role,str) or not role or role in settings:raise ValueError('invalid configured learning role identity')
        conf=agent.get('learning_config');spec=agent.get('learning_runtime')
        settings[role]=prepare_role_learning_settings(conf,spec)
        capacities.add(spec['audit_capacity'])
        methods={m.get('id'):m.get('method') for m in agent.get('architecture',[]) if m.get('selected') is not False}
        if methods.get('learning')!=METHODS[conf['method_id']]['label']:
            raise ValueError('confirmed learner method differs from architecture projection')
        if methods.get('decision')!='运行时规则决策' and not (coop and methods.get('decision')=='LLM-driven'):
            raise ValueError('configured learning currently requires rule decisions')
        participates=bool(agent.get('capabilities',{}).get('emotion') and agent.get('emotionState',{}).get('participates_in_operation'))
        if spec['encoding']['emotion_participates'] and not participates:raise ValueError('confirmed learning PAD participation mismatch')
    if not settings:raise ValueError('configured learning contract requires a learning-enabled role')
    if len(capacities)!=1:raise ValueError('per-run learning audit capacity must agree across roles')
    return settings,capacities.pop()


def configured_learning_catalog():
    return [dict(model='individual',module='learning',method=data['label'],method_id=method,
        module_path='crowdsim.modeling_algorithm.individual_model.learning.runtime_controller',
        implemented=True,runtime_ready=True,execution_contract=CONFIGURED_LEARNING_CONTRACT,
        implementation_version='1',parameters=deepcopy(data['parameters'])) for method,data in METHODS.items()]
