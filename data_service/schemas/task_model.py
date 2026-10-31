"""Requirements and actor relationships owned by one analysis task."""
import re

OUTPUTS = {'density', 'bottlenecks', 'exits', 'comparison', 'interventions', 'report'}
REQUIREMENT_FIELDS = {'analysis_goal', 'key_questions', 'known_conditions', 'expected_outputs'}

COGNITIVE_METHODS = {'OODA', 'Organization', 'CooperativeGame'}
COGNITIVE_FIELDS = {'observe', 'orient', 'decide', 'act', 'role', 'sops', 'interests', 'bargaining_rules'}


def validate_cognitive_models(value, group_ids):
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError('认知模型最多包含30类主体')
    ids = set()
    for item in value:
        if not isinstance(item, dict) or not {'id', 'method'} <= set(item) or set(item) - {'id', 'method', *COGNITIVE_FIELDS}:
            raise ValueError('认知模型须包含主体ID、模型类型及受支持的认知字段')
        if not isinstance(item['id'], str) or item['id'] not in group_ids or item['id'] in ids or item['method'] not in COGNITIVE_METHODS:
            raise ValueError('认知模型须引用不重复的已有主体，并选择有效模型类型')
        ids.add(item['id'])
        if any(not isinstance(item.get(key, ''), str) or len(item.get(key, '')) > 1000 for key in COGNITIVE_FIELDS):
            raise ValueError('每项认知规则必须为不超过1000字的文本')


def validate_requirements(value):
    if not isinstance(value, dict):
        raise ValueError('分析需求必须为对象')
    unknown = set(value) - REQUIREMENT_FIELDS
    if unknown:
        raise ValueError('分析需求包含未知字段：' + '、'.join(sorted(unknown)) + '；研判方案应单独保存在 research_plan 中')
    for key, limit in (('analysis_goal', 4000), ('key_questions', 4000), ('known_conditions', 8000)):
        if not isinstance(value.get(key, ''), str) or len(value.get(key, '')) > limit:
            raise ValueError(f'{key} 必须为不超过 {limit} 字的文本')
    outputs = value.get('expected_outputs', [])
    if not isinstance(outputs, list) or any(not isinstance(item, str) or item not in OUTPUTS for item in outputs) or len(set(outputs)) != len(outputs):
        raise ValueError('请使用有效且不重复的预期输出选项')


def validate_agent_model(value):
    if not isinstance(value, dict) or set(value) - {'groups', 'relationships', 'studio'} or not {'groups', 'relationships'} <= set(value):
        raise ValueError('智能体模型须包含 groups 和 relationships')
    groups, links = value['groups'], value['relationships']
    if not isinstance(groups, list) or len(groups) > 30 or not isinstance(links, list) or len(links) > 80:
        raise ValueError('最多配置 30 类主体与 80 条关系')
    identifiers = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {'id', 'name', 'role', 'goal'}:
            raise ValueError('主体须包含 id、name、role、goal')
        identifier = group['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier) or identifier in identifiers:
            raise ValueError('主体 ID 必须为不重复的英文、数字、下划线或短横线，长度不超过 64')
        identifiers.add(identifier)
        for key, limit in (('name', 100), ('role', 100), ('goal', 1000)):
            if not isinstance(group[key], str) or len(group[key]) > limit or (key == 'name' and not group[key].strip()):
                raise ValueError(f'主体 {key} 须为长度不超过 {limit} 的有效文本')
    link_ids = set()
    for link in links:
        if not isinstance(link, dict) or set(link) != {'id', 'source', 'target', 'relationship', 'description'}:
            raise ValueError('关系须包含 id、source、target、relationship、description')
        identifier = link['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier) or identifier in link_ids:
            raise ValueError('关系 ID 须有效且不重复，长度不超过 64')
        link_ids.add(identifier)
        if not isinstance(link['source'], str) or not isinstance(link['target'], str) or link['source'] not in identifiers or link['target'] not in identifiers or link['source'] == link['target']:
            raise ValueError('关系须连接两个不同的已有主体')
        for key, limit in (('relationship', 100), ('description', 1000)):
            if not isinstance(link[key], str) or len(link[key]) > limit or (key == 'relationship' and not link[key].strip()):
                raise ValueError(f'关系 {key} 须为长度不超过 {limit} 的有效文本')
    if 'studio' in value:
        studio = value['studio']
        if not isinstance(studio, dict) or set(studio) - {'agents', 'relationships', 'experimentConfig', 'llmOptimization'}:
            raise ValueError('原版智能体详情配置无效')
        agents, relations = studio.get('agents', []), studio.get('relationships', [])
        if not isinstance(agents, list) or len(agents) > 30 or not isinstance(relations, list) or len(relations) > 80:
            raise ValueError('原版详情最多包含30类主体与80条关系')
        if any(not isinstance(agent, dict) or not isinstance(agent.get('id'), str) for agent in agents) or len({agent['id'] for agent in agents}) != len(agents):
            raise ValueError('原版详情中的主体标识无效或重复')
        if any(not isinstance(link, dict) for link in relations):
            raise ValueError('原版详情中的关系无效')
