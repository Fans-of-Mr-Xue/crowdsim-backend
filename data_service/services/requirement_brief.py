"""Track the requirement workflow from the editable draft on every turn."""
from copy import deepcopy

DEFERRED_NOTE = '用户暂时无法提供，保留待补充，先核对其他输入。'


def projected_context(context, changes=None):
    result = deepcopy(context)
    for field in result['fields']:
        if field['id'] in (changes or {}):
            field['value'] = deepcopy(changes[field['id']])
    return result


def requirement_brief(context):
    fields = {item['id']: item.get('value') for item in context['fields']}
    plan = fields.get('requirement.plan') or {}
    state = (context.get('state') or {}).get('task-definition') or {}
    region = state.get('region') or {}
    needs = plan.get('data_needs', [])
    groups = {'available': [], 'retrieved': [], 'pending': [], 'assumed': [], 'deferred': []}
    for row in needs:
        status = row.get('status', 'pending')
        if status in ('available', 'retrieved') and (not row.get('detail', '').strip() or row.get('source') in (None, 'unknown')):
            status = 'pending'
        if status == 'assumed' and not row.get('detail', '').strip():
            status = 'pending'
        if status == 'pending' and DEFERRED_NOTE in row.get('detail', ''):
            status = 'deferred'
        groups[status if status in groups else 'pending'].append(row)
    boundary = bool(fields.get('task.environment') or region.get('geometry'))
    # A selected/drawn named boundary is a valid location; a missing redundant
    # location-text field must not silently disable all later guidance.
    location = bool(fields.get('task.location') or (boundary and region.get('name')) or fields.get('task.environment'))
    milestones = [
        {'id': 'goal', 'label': '研究目标', 'complete': bool(fields.get('requirement.analysis_goal') and ('task.title' not in fields or fields.get('task.title')))},
        {'id': 'scope', 'label': '时间与区域', 'complete': bool(fields.get('task.timeWindow') and location and boundary)},
        {'id': 'cases', 'label': '参考案例', 'optional': True, 'complete': bool(fields.get('task.references'))},
        {'id': 'outputs', 'label': '预期输出', 'complete': bool(fields.get('requirement.expected_outputs') and plan.get('indicators') and all(row.get('name', '').strip() and row.get('definition', '').strip() for row in plan['indicators']))},
        {'id': 'approach', 'label': '模型选择', 'complete': bool(plan.get('experiment', {}).get('model_ids') and plan.get('steps') and all(all(row.get(key, '').strip() for key in ('name', 'inputs', 'output')) for row in plan['steps']))},
        {'id': 'inputs', 'label': '通用输入', 'complete': bool(needs) and not groups['pending'] and not groups['retrieved']},
    ]
    current = next((row for row in milestones if not row['complete'] and not row.get('optional')), None)
    priorities = []
    if not fields.get('requirement.analysis_goal'):
        priorities.append('明确研究目标')
    if 'task.title' in fields and not fields.get('task.title'):
        priorities.append('明确任务名称')
    if not fields.get('task.timeWindow'):
        priorities.append('确认完整分析时间窗；不猜年份')
    if not location:
        priorities.append('确认研究地点')
    if not boundary:
        priorities.append('确认使用环境库区域或自行圈定边界')
    if not milestones[3]['complete']:
        priorities.append('确认预期输出及指标定义')
    if not milestones[4]['complete']:
        priorities.append('通过确认卡选择比较模型，并同步右侧模型选择')
    priorities.extend('核对已有资料：' + row['name'] for row in groups['retrieved'])
    priorities.extend('补充输入：' + row['name'] for row in groups['pending'])
    if not needs:
        priorities.append('按所选模型列出通用输入，再逐项核对')
    return {
        'detail_tabs': ['任务概览', '参考依据'],
        'overview_sections': ['研究目标', '活动与范围', '通用输入', '预期输出', '模型选择'],
        'workflow': milestones,
        'current_stage': current['label'] if current else '最终核对',
        'ready_for_review': current is None,
        'input_counts': {key: len(rows) for key, rows in groups.items()},
        'priorities': priorities,
        'awaiting_review': groups['retrieved'], 'missing_inputs': groups['pending'],
        'deferred_inputs': groups['deferred'], 'assumptions': groups['assumed'],
        'user_questions': plan.get('open_questions', []),
        'stage_boundary': '本页确认目标范围、案例、预期输出、模型和通用输入。05为模型选择；参数、重复次数和运行由独立实验控制页面负责。',
        'retrieval_capability': '只有context.retrieved_cases存在才执行了本轮案例及文献元数据检索；未接入文献全文检索和外网采集。',
        'confirmation_policy': 'available是用户确认且有内容与依据；retrieved是已获取待核对；pending是待补；assumed是明确假设。暂缺资料继续保持pending，不因推进而变成available。',
        'turn_policy': '完成本轮实际修改后重新核对workflow和输入状态，回复末尾必须给出下一步具体操作及对应卡片。不得只复述已更新。模型建议用可执行确认卡；基本完备时汇总假设与遗留缺口，请用户确认需求后进入领域模型，不自动跳转。',
    }
