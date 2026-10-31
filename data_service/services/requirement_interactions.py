"""Bind consent to exact edits and keep requirement dialogue in its stage."""
from copy import deepcopy
import re

from ..repositories.datasets import DatasetConflictError
from ..schemas.page_assistant import validate_plan, validate_editor_operations


def task_binding(context):
    return {'task_id': (context.get('task_reference') or {}).get('id'), 'example_mode': bool(context.get('example_mode'))}


def resolve_proposal(decision, context, task):
    if not decision or decision.get('kind') != 'proposal':
        return None
    if decision['choice'] == 'reject':
        return validate_plan({'reply': f'已拒绝“{decision["title"]}”，该建议未应用。你可以在对话中说明希望怎样调整。'}, context)
    if not decision.get('changes') or 'base_values' not in decision:
        # Old, text-only cards cannot safely be interpreted as executable consent.
        raise ValueError('这是一条旧版文字建议，未绑定具体修改。请在对话中要求重新生成建议后再确认')
    if decision.get('subject_id') == 'progress-methods' and 'Weidmann' in str(decision.get('changes', {})):
        return validate_plan({'reply': '这条旧模型建议已被新版三模型目录替代，先核对下面的说明与新建议。'}, context)
    fields = {row['id']: row.get('value') for row in context['fields']}
    if decision.get('task_binding') != task_binding(context) or any(key not in fields or fields[key] != value for key, value in decision['base_values'].items()):
        raise DatasetConflictError('建议涉及的内容已经修改，旧建议未应用。请在对话中要求基于当前详情更新建议')
    plan = validate_editor_operations(validate_plan({'reply': f'已按你同意的“{decision["title"]}”提交对应修改；右侧更新情况见执行回执。', 'changes': deepcopy(decision['changes'])}, context), context, task)
    return plan


def validate_requirement_interactions(plan, context, task, request):
    action = (plan.get('action') or {}).get('id')
    decision = context.get('user_decision')
    # Local consent never authorizes saving, switching tasks, or advancing stages.
    if decision and action:
        raise ValueError('当前按钮只确认卡片内容，不允许保存、跳转或其他操作')
    intents = {
        'task.confirm': r'(确认(?:整个|全部|当前)?(?:任务|需求|研究安排)|进入领域模型|下一步|下一阶段)',
        'task.save': r'(保存|存为|存一下)',
        'task.choose': r'(切换|打开|选择).{0,20}任务',
        'task.draw-boundary': r'(画|绘|框|标|打开|编辑|调整|选择).{0,12}(地图|范围|边界)',
    }
    if action in intents and (re.search(r'(不要|别|不需要|暂不|不用)', request) or not re.search(intents[action], request)):
        raise ValueError('用户没有明确要求这项流程操作，action须为空；补充信息使用input_requests定位')
    if 'task.environment' in plan['changes']:
        raise ValueError('环境库区域须通过服务端环境确认卡片采纳，不能直接写入task.environment')
    fields = {row['id']: row.get('value') for row in context['fields']}
    current_plan = plan['changes'].get('requirement.plan', fields.get('requirement.plan')) or {}
    for request_item in plan['input_requests']:
        if request_item.get('input_name') and not any(row.get('name') == request_item['input_name'] for row in current_plan.get('data_needs', [])):
            raise ValueError('input_name须对应当前或本轮新增的输入清单项；否则只定位inputs区域')
    pending_fields = set()
    for card in plan['confirmations']:
        card['task_binding'] = task_binding(context)
        if card['kind'] != 'proposal':
            continue
        effect = validate_editor_operations(validate_plan({'reply': card['detail'], 'changes': card['changes']}, context), context, task)
        # These effects need the dedicated case/map consent paths instead.
        if set(effect['changes']) & {'task.references', 'task.reference_notes', 'task.environment'}:
            raise ValueError('案例采纳使用case确认项；研究范围通过右侧定位核对')
        # An explicit user fact can be recorded now while an expansion of that
        # fact remains a separate proposal, bound to the newly recorded value.
        recorded_facts = {key for key, value in context.get('confirmed_user_fields', {}).items()
                          if plan['changes'].get(key) == value and effect['changes'].get(key) != value}
        if set(effect['changes']) & ((set(plan['changes']) - recorded_facts) | pending_fields):
            raise ValueError('待确认修改不能提前写入changes，也不能在同轮多张卡片中重复修改同一字段')
        pending_fields.update(effect['changes'])
        card['changes'] = effect['changes']
        card['base_values'] = {key: deepcopy(plan['changes'].get(key, fields[key])) for key in effect['changes']}
        card['task_binding'] = task_binding(context)
    if plan['confirmations'] and action:
        raise ValueError('仍有待确认建议时不能自动执行流程操作')
    return plan


def needs_case_search(content, context):
    if context.get('user_decision'):
        return False
    return bool(re.search(r'案例|事例|文献|论文|参考|检索|查找|借鉴|类似|相似', content))
