"""Match actual environment-library records and bind adoption to explicit consent."""
from copy import deepcopy
import re
from uuid import uuid4

from ..repositories.datasets import DatasetConflictError
from ..schemas.page_assistant import validate_plan, validate_editor_operations
from .requirement_interactions import task_binding


def normalized(text):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', str(text or '')).lower()


def aliases(profile):
    title = re.sub(r'(区域|环境|区域环境)?画像$', '', profile.get('title', ''))
    parts = [title, *re.split(r'[·/，,|]', profile.get('locationLabel', ''))]
    return [normalized(part) for part in parts if len(normalized(part)) >= 4]


def match_score(profile, text):
    text = normalized(text)
    return max((len(name) for name in aliases(profile) if name in text), default=0)


class RequirementEnvironmentSkill:
    def __init__(self, repository):
        self.repository = repository

    def search(self, content, context, history):
        fields = {row['id']: row for row in context['fields']}
        field = fields.get('task.environment')
        if not field or not field.get('enabled', True):
            return []
        allowed = field.get('schema', {}).get('enum', [])
        if not allowed:
            return []
        # Query Mongo's live library, including user-created profiles. No static
        # seed list, inferred geometry, or remote geocoding is used here.
        profiles = list(self.repository.collection.find(
            {'_id': {'$in': allowed}, 'is_deleted': {'$ne': True}},
            {'title': 1, 'locationLabel': 1, 'description': 1, 'revision': 1, 'status': 1, 'geometry': 1}))
        location = fields.get('task.location', {}).get('value', '')
        matches = [(match_score(row, content), row) for row in profiles]
        if not any(score for score, _ in matches):
            matches = [(match_score(row, location), row) for row in profiles]
        retry = bool(re.search(r'(重新|再次).{0,8}(检索|查找|搜索|匹配|推荐).{0,8}(环境|区域)', content))
        candidates = []
        for score, row in sorted(matches, key=lambda item: (-item[0], item[1].get('title', ''))):
            if not score or not row.get('geometry') or row['_id'] == field.get('value'):
                continue
            # A pending or rejected offer for this same task/location should
            # not be repeated every time the user supplies another field.
            offered = [card for message in history for card in message.get('plan', {}).get('confirmations', [])
                       if card.get('kind') == 'environment' and card.get('subject_id') == row['_id']
                       and card.get('environment_revision') == row.get('revision')
                       and card.get('task_binding') == task_binding(context)
                       and card.get('base_values', {}).get('task.location') == location
                       and card.get('base_values', {}).get('task.environment') == field.get('value')]
            if offered and not retry:
                continue
            candidates.append({'id': row['_id'], **{key: row.get(key) for key in ('title', 'locationLabel', 'description', 'revision', 'status')}})
        return candidates

    def attach(self, plan, context):
        fields = {row['id']: row.get('value') for row in context['fields']}
        location = plan['changes'].get('task.location', fields.get('task.location', ''))
        for candidate in context.get('environment_candidates', []):
            # If the model records a different location this turn, never attach
            # a match for the previous task location.
            if location and not match_score(candidate, location):
                continue
            detail = '可使用此区域，也可自己圈定。使用后同步边界与环境画像，保留研究目标和时间；画像资料仍需核对。'
            plan['confirmations'].append({
                'id': uuid4().hex, 'kind': 'environment', 'subject_id': candidate['id'],
                'title': f"使用“{candidate['title']}”？", 'detail': detail,
                'changes': {'task.environment': candidate['id']},
                'base_values': {key: deepcopy(plan['changes'].get(key, fields.get(key))) for key in ('task.environment', 'task.location')},
                'task_binding': task_binding(context), 'environment_revision': candidate['revision'],
            })
        # Location and time are independent choices. Never defer the time
        # selector just because one or more environment cards are present.
        if context.get('environment_candidates') and not plan['changes'].get('task.timeWindow', fields.get('task.timeWindow')) and any(
                row.get('id') == 'scope' and row.get('enabled', True) for row in context.get('input_targets', [])):
            time_card = next((row for row in plan['input_requests'] if row['target'] == 'scope' and (row.get('widget') == 'time-window' or re.search(r'时间|时段|日期', row['title']))), None)
            if time_card:
                time_card['widget'] = 'time-window'
            else:
                plan['input_requests'].append({'target': 'scope', 'widget': 'time-window', 'title': '补充研究时间窗', 'detail': '请选择完整的开始和结束日期、时间；跨年时请核对年份。'})
        return plan

    def resolve(self, decision, context, task):
        if decision['choice'] == 'reject':
            return validate_plan({'reply': '暂不使用这个环境。保留当前研究地点和范围，你可以继续说明需要的区域。'}, context)
        fields = {row['id']: row.get('value') for row in context['fields']}
        if decision.get('task_binding') != task_binding(context) or not decision.get('base_values') or any(
                key not in fields or fields[key] != value for key, value in decision['base_values'].items()):
            raise DatasetConflictError('研究地点或范围已变化，请重新匹配环境后确认')
        identifier = decision['subject_id']
        if decision.get('changes') != {'task.environment': identifier}:
            raise ValueError('环境确认只能绑定该环境，不得附带其他修改')
        profile = self.repository.get(identifier)
        if not profile or profile.get('revision') != decision.get('environment_revision'):
            raise DatasetConflictError('该环境已更新或删除，请重新检索环境后确认')
        if decision['choice'] == 'customize':
            plan = validate_editor_operations(validate_plan({
                'reply': '将打开地图供你自己圈定范围；只有在地图中确认后才更新右侧边界，当前未采用候选区域。',
                'action': {'id': 'task.draw-boundary', 'parameters': {}}}, context), context, task)
            plan['environment_preview'] = {'id': identifier, 'revision': profile['revision']}
            return plan
        plan = validate_editor_operations(validate_plan({
            'reply': f"已按你的选择提交“{profile['title']}”的已有边界与环境画像，右侧更新情况见执行回执。研究目标和时间保持不变。",
            'changes': {'task.environment': identifier}}, context), context, task)
        plan['environment_selection'] = {'id': identifier, 'revision': profile['revision']}
        return plan
