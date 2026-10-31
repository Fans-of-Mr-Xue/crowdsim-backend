"""A bounded, editable research brief; it does not execute a prediction."""
from .page_assistant import validate_value
from ..prediction.consultation_catalog import experiment_schema


def text(limit):
    return {'type': 'string', 'maxLength': limit}


def rows(properties, limit):
    return {'type': 'array', 'maxItems': limit, 'items': {
        'type': 'object', 'properties': properties, 'required': list(properties)}}


PLAN_SCHEMA = {'type': 'object', 'properties': {
    'experiment': experiment_schema(),
    'objective': text(4000),
    'indicators': rows({'name': text(100), 'unit': text(40), 'definition': text(600), 'method': text(600), 'input': text(600)}, 8),
    'data_needs': rows({'name': text(120), 'source': {'type': 'string', 'enum': ['user', 'knowledge', 'online', 'unknown']},
                        'status': {'type': 'string', 'enum': ['available', 'retrieved', 'pending', 'assumed']}, 'detail': text(800)}, 12),
    'steps': rows({'name': text(120), 'method': {'type': 'string', 'enum': ['preparation', 'professional', 'llm', 'hybrid']},
                   'inputs': text(800), 'output': text(800)}, 8),
    'assumptions': {'type': 'array', 'maxItems': 10, 'items': text(600)},
    'open_questions': {'type': 'array', 'maxItems': 10, 'items': text(600)},
}, 'required': ['objective', 'indicators', 'data_needs', 'steps', 'assumptions', 'open_questions']}


def validate_research_plan(value, *, confirmed=False):
    validate_value(value, PLAN_SCHEMA, '研判方案')
    settings = value.get('experiment', {}).get('settings', {})
    if settings.get('warning_density_p_m2', 4) >= settings.get('jam_density_p_m2', 6):
        raise ValueError('密度提示阈值应小于拥挤密度上限')
    for row in value['data_needs']:
        if row['status'] in ('available', 'retrieved') and (not row['name'].strip() or not row['detail'].strip() or row['source'] == 'unknown'):
            raise ValueError('已获取或已确认的输入必须填写名称、来源及具体依据；尚无依据请标为待补充')
    required_rows = (('indicators', ('name', 'definition')), ('data_needs', ('name',)), ('steps', ('name', 'inputs', 'output'))) if confirmed else ()
    for kind, required in required_rows:
        for row in value[kind]:
            if any(not row[key].strip() for key in required):
                raise ValueError('请补全方案中的名称、指标定义，以及步骤的输入和输出；暂缺信息请明确写待补充')
    if confirmed and (not value['objective'].strip() or not value['indicators'] or not value['steps']):
        raise ValueError('确认需求前，请形成方案目标、至少一个有定义的指标和一个包含输入与输出的研判步骤')
    return value
