"""Bounded, explicit decisions attached to an assistant turn."""
def confirmations(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError('confirmations须为确认卡片数组')
    result, seen = [], set()
    for row in value:
        if not isinstance(row, dict) or not {'kind', 'subject_id', 'title', 'detail'} <= set(row) or set(row) - {'kind', 'subject_id', 'title', 'detail', 'changes'}:
            raise ValueError('确认项须包含kind、subject_id、title、detail')
        if row['kind'] not in ('case', 'proposal'):
            raise ValueError('确认类型须为case或proposal')
        if row['kind'] == 'proposal' and (not isinstance(row.get('changes'), dict) or not row['changes']):
            raise ValueError('proposal必须绑定同意后实际应用的changes；仅需补充信息时使用input_requests')
        if row['kind'] == 'case' and 'changes' in row:
            raise ValueError('案例确认只能采纳该案例的具体借鉴范围')
        for key, maximum in [('subject_id', 200), ('title', 200), ('detail', 1200)]:
            if not isinstance(row[key], str) or not row[key].strip() or len(row[key]) > maximum:
                raise ValueError('确认项名称、对象和说明须为有效文本')
        identity = (row['kind'], row['subject_id'])
        if identity in seen:
            raise ValueError('同一建议不能重复确认')
        seen.add(identity)
        result.append(row)
    return result


def resolve_decision(messages, payload):
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) != {'message_id', 'confirmation_id', 'choice'} or payload['choice'] not in ('accept', 'reject', 'customize'):
        raise ValueError('确认选择无效')
    if any(message.get('decision', {}).get('message_id') == payload['message_id'] and message.get('decision', {}).get('confirmation_id') == payload['confirmation_id'] for message in messages):
        raise ValueError('这一项已经选择，请通过对话说明需要调整的意见')
    message = next((item for item in messages if item.get('role') == 'assistant' and item.get('id') == payload['message_id']), None)
    row = next((item for item in (message or {}).get('plan', {}).get('confirmations', []) if item.get('id') == payload['confirmation_id']), None)
    if row is None:
        raise ValueError('这项确认已不存在，请刷新对话')
    if payload['choice'] == 'customize' and row['kind'] != 'environment':
        raise ValueError('自定义范围仅用于环境匹配卡片')
    index = messages.index(message)
    if any(item.get('kind') == row['kind'] and item.get('subject_id') == row['subject_id']
           for later in messages[index + 1:] for item in later.get('plan', {}).get('confirmations', [])):
        raise ValueError('这项建议已有更新，请选择最新的确认项')
    # Effects come from the saved assistant card, never from the click payload.
    return {**payload, **{key: row[key] for key in ('kind', 'subject_id', 'title', 'detail', 'changes', 'base_values', 'task_binding', 'environment_revision') if key in row}}


def decision_text(decision):
    label = {'accept': '同意', 'reject': '不同意', 'customize': '自己圈定范围'}[decision['choice']]
    return f"我选择{label}：{decision['title']}。具体内容：{decision['detail']}"
