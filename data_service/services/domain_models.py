"""Bootstrap task-owned actors from live subject templates without overwriting edits."""
import copy
import hashlib

from ..repositories.subjects import SubjectRepository
from ..repositories.analysis_tasks import AnalysisTaskRepository
from ..repositories.datasets import DatasetConflictError
from ..repositories.knowledge_documents import now

TASK_FIELDS = ('title', 'summary', 'event_id', 'event_label', 'time_window', 'template_case', 'status', 'region', 'population', 'references', 'components')


class DomainModelService:
    def __init__(self, database):
        self.tasks = AnalysisTaskRepository(database)
        self.subjects = SubjectRepository(database)

    def read(self, identifier, revision):
        task = self.tasks.get(identifier)
        if task is None:
            raise KeyError(identifier)
        if task['revision'] != revision:
            raise DatasetConflictError('任务已更新，请重新读取领域模型')
        return task

    def initialize(self, identifier, revision):
        task = self.read(identifier, revision)
        components = task['components']
        model = components.get('agent_model') or {}
        # An intentionally emptied or manually configured model is still owned
        # by the user. Reopening must not repopulate it or create more versions.
        if components.get('domain_model_review') or model.get('groups') or model.get('relationships') or model.get('studio', {}).get('agents'):
            return {'task': task, 'created': False}
        subjects = [s for s in self.subjects.list()['items'] if s.get('agentClass', {}).get('classId')]
        context = ' '.join(str(task.get(k, '')) for k in ('title', 'summary', 'event_id', 'event_label'))
        include_vehicles = any(word in context for word in ('内涝', '洪水', '车辆', '交通事故', 'flood'))
        candidates = [s for s in subjects if s.get('topic') in ('人群主体', '协同角色') or (include_vehicles and s.get('topic') == '车辆主体')]
        # Prefer templates whose explicitly declared scene matches this task.
        def score(subject):
            declared = ' '.join(value for label, value in subject.get('details', []) if label == '应用场景')
            return sum(word in context and word in declared for word in ('上海', '外滩', '人群聚集', '内涝', '洪水'))
        candidates = sorted(candidates, key=score, reverse=True)[:20]
        if not candidates:
            raise ValueError('主体库中没有适用的主体模板，请先在知识库的主体库中添加人群或协同角色，再重新读取本页')
        groups, agents, refs = [], [], []
        crowd, support = [], {}
        for subject in candidates:
            agent_class = subject['agentClass']
            portrait = subject.get('agentPortrait') or {}
            actor_id = 'actor_' + hashlib.sha256(subject['id'].encode()).hexdigest()[:20]
            is_crowd = subject['topic'] == '人群主体'
            role = (portrait.get('role') or agent_class.get('description') or subject['name'])[:100]
            goal = ('在任务范围内完成活动，观察拥堵并依据现场引导选择通行路线。' if is_crowd else subject.get('summary') or role)[:1000]
            groups.append({'id': actor_id, 'name': subject['name'][:100], 'role': role, 'goal': goal})
            facts, details = dict(portrait.get('facts', [])), dict(subject.get('details', []))
            method = 'OODA' if is_crowd or subject['topic'] == '车辆主体' else 'Organization'
            cognitive = {'observe': '观察周边密度、通道可用性、标识与现场广播。', 'orient': '结合目的地、场地熟悉程度和同伴状态判断可行路线。',
                         'decide': '在继续通行、等待和接受分流引导之间作出选择。', 'act': '按所选路线行动，遇到阻塞时重新观察。'} if method == 'OODA' else {'role': role, 'SOPs_as_Text': goal}
            if subject['topic'] == '车辆主体':
                cognitive = {'observe': '获取道路通行、积水和交通管制信息。', 'orient': '结合当前位置、目的地及车辆通行约束评估道路可用性。',
                             'decide': '选择通行、等待、绕行或执行调度，具体触发条件待确认。', 'act': '按获准路线或调度指令行动，并更新道路状态。'}
            agents.append({'id': actor_id, 'name': subject['name'][:100], 'type': agent_class['classId'], 'role': role, 'goal': goal, 'count': 1,
                           'color': portrait.get('tone') or agent_class.get('color') or '#5793a5',
                           'subjectTemplate': {'id': subject['id'], 'revision': subject['revision'], 'classId': agent_class['classId'], 'name': subject['name'], 'snapshot': copy.deepcopy(subject)},
                           'agentPortrait': copy.deepcopy(portrait), 'behaviorRules': copy.deepcopy(portrait.get('metrics', [])), 'customAttributes': [],
                           'traits': {'nationality': facts.get('国籍 / 地区') or details.get('国籍 / 地区', ''), 'age': details.get('年龄', ''), 'gender': details.get('性别', ''),
                                      'purpose': facts.get('主要出行目的', ''), 'familiarity': facts.get('路线熟悉程度', '')},
                           'llmConfig': {'model': '', 'systemPrompt': ''}, 'modelingMethod': method, 'cognitiveModel': cognitive})
            refs.append({'id': subject['id'], 'revision': subject['revision'], 'name': subject['name'], 'actor_id': actor_id, 'match': '场景关联模板' if score(subject) else '主体库候选模板，适用性待确认'})
            if is_crowd:
                crowd.append(actor_id)
            else:
                support.setdefault(agent_class['classId'], actor_id)
        links = []
        def link(source, target, label, description):
            if source and target and source != target:
                links.append({'id': f'relation_{len(links) + 1}', 'source': source, 'target': target, 'relationship': label, 'description': '建议关系，待用户确认。' + description})
        command, control, monitor, transport = [support.get(key) for key in ('PolicyPublisher', 'TrafficControl', 'InfoPublisher', 'BusDispatcher')]
        link(monitor, command, '态势上报', '监测与信息发布角色向指挥角色提供现场状态。')
        link(command, control, '分流协同', '指挥角色与现场管制角色协调通道使用及分流。')
        link(command, transport, '交通接驳协同', '协调活动期间的外部交通接驳，实际职责需核对。')
        for actor_id in crowd:
            link(control or monitor, actor_id, '现场引导', '人群接收现场引导；服从程度和生效条件尚需确认。')
        components['agent_model'] = {'groups': groups, 'relationships': links, 'studio': {'agents': agents, 'relationships': copy.deepcopy(links)}}
        components['domain_model_review'] = {'status': 'pending_confirmation', 'origin': 'subject_library', 'generator_version': 'subject-bootstrap-1',
            'generated_at': now(), 'sources': refs, 'assumptions': ['主体来自数据库主体库的固定版本；候选角色不代表现场实际人员已核实。',
            '认知流程、行为目标和协同关系为待确认初稿；模板行为参数仍需场景标定。', '每类先创建一个代表性主体，未推断实际人数或人口构成，也未修改人群比例与预测参数。']}
        try:
            saved = self.tasks.save({key: task[key] for key in TASK_FIELDS}, identifier, revision)
        except DatasetConflictError:
            # Another tab may have initialized the same revision first.
            current = self.tasks.get(identifier)
            if current and current.get('components', {}).get('domain_model_review'):
                return {'task': current, 'created': False}
            raise
        return {'task': saved, 'created': True}

    def confirm(self, identifier, revision, definition):
        current = self.read(identifier, revision)
        payload = copy.deepcopy(definition or {key: current[key] for key in TASK_FIELDS})
        # Provenance is retained from the saved task, never invented by a client.
        review = copy.deepcopy(current['components'].get('domain_model_review') or {'origin': 'manual', 'sources': []})
        review.update(status='confirmed', confirmed_at=now())
        payload.setdefault('components', {})['domain_model_review'] = review
        return self.tasks.save(payload, identifier, revision, confirm_domain=True)
