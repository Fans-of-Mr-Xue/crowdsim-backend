"""Keep case adoption gated by a choice on the actual recommendation."""
from uuid import uuid4


def apply_case_decision(raw, context):
    decision = context.get('user_decision')
    if not decision or decision['kind'] != 'case':
        return raw
    fields = {row['id']: row.get('value') for row in context['fields']}
    changes = raw.setdefault('changes', {})
    if not isinstance(changes, dict):
        return raw
    identifier = decision['subject_id']
    adopted = list(fields.get('task.references') or [])
    if decision['choice'] == 'accept':
        if identifier not in adopted:
            adopted.append(identifier)
        # The button accepts this displayed scope, not a broader rewrite
        # invented during the next model response.
        changes['task.reference_notes'] = [{'id': identifier, 'adoption_note': decision['detail']}]
    else:
        adopted = [value for value in adopted if value != identifier]
        changes.pop('task.reference_notes', None)
    changes['task.references'] = adopted
    return raw


def validate_case_choices(plan, context):
    fields = {row['id']: row.get('value') for row in context['fields']}
    adopted = set(fields.get('task.references') or [])
    choice = context.get('user_decision') or {}
    allowed = adopted | ({choice['subject_id']} if choice.get('kind') == 'case' and choice.get('choice') == 'accept' else set())
    if set(plan['changes'].get('task.references', [])) - allowed:
        raise ValueError('新案例必须先通过confirmations提出具体借鉴建议，用户点击同意后才能加入task.references；本轮未确认的案例不要写入changes')
    candidates = {row['id'] for row in context.get('retrieved_cases', {}).get('items', [])}
    for discussion in context.get('requirement_dialogue', {}).get('previous_discussions_newest_first', []):
        candidates.update(row['id'] for row in discussion.get('case_candidates', []))
    candidates.update(adopted)
    if context.get('example_mode'):
        state = (context.get('state') or {}).get('task-definition') or {}
        candidates.update(row['id'] for row in state.get('visible_case_candidates', []) + state.get('example_case_catalog', []))
    if choice.get('kind') == 'case':
        candidates.add(choice['subject_id'])
    plan['confirmations'] = [row for row in plan['confirmations']
                             if (row['kind'], row['subject_id']) != (choice.get('kind'), choice.get('subject_id'))]
    for row in plan['confirmations']:
        if row['kind'] == 'case' and row['subject_id'] not in candidates:
            raise ValueError('确认按钮只能引用真实检索到或已采纳的案例ID')
    # A model occasionally explains a candidate in prose but omits the control.
    # Add a grounded control only for explicitly named, not-yet-adopted cases.
    blocked = set(context.get('rejected_case_ids') or [])
    offered = {row['subject_id'] for row in plan['confirmations'] if row['kind'] == 'case'}
    for row in context.get('retrieved_cases', {}).get('items', []):
        if row['title'] and row['title'] in plan['reply'] and row['id'] not in allowed | blocked | offered:
            plan['confirmations'].append({'kind': 'case', 'subject_id': row['id'], 'title': row['title'], 'detail': row['adoption_note'][:1200]})
    for row in plan['confirmations']:
        row['id'] = uuid4().hex
