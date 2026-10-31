"""Ground dialogue in saved tasks and generate validated edits for detail views."""
import asyncio
import json
import logging

from ..connectors.llm import ModelError
from ..prediction.inputs import BOUNDS
from ..prompts import load_prompt
from ..repositories.analysis_tasks import AnalysisTaskRepository
from ..repositories.datasets import DatasetConflictError
from ..schemas.requirement_interactions import input_request_contract
from ..schemas.page_assistant import identifier, validate_context, validate_editor_operations, validate_plan, normalize_editor_changes
from .requirement_cases import RequirementCaseSearch
from .requirement_brief import requirement_brief
from .requirement_environments import RequirementEnvironmentSkill
from .requirement_skills import time_window_skill, experiment_parameter_skill, grounded_requirement_changes
from .requirement_dialogue import requirement_dialogue
from .requirement_confirmations import apply_case_decision, validate_case_choices
from .requirement_interactions import resolve_proposal, validate_requirement_interactions, needs_case_search, task_binding
from .requirement_progression import advance_requirements, asks_to_continue, explain_model_proposals, defer_input_skill, finish_requirement_turn
from .requirement_card_budget import limit_requirement_cards

logger = logging.getLogger(__name__)


class PageAssistantService:
    def __init__(self, database, gateway):
        self.tasks = AnalysisTaskRepository(database)
        self.gateway = gateway
        self.case_search = RequirementCaseSearch(database)
        self.environment_skill = RequirementEnvironmentSkill(self.tasks.environments)

    async def read_task(self, reference):
        if not reference:
            return None
        identifier(reference['id'])
        if type(reference.get('revision')) is not int or reference['revision'] < 1:
            raise ValueError('请重新读取分析任务版本')
        task = await asyncio.to_thread(self.tasks.get, reference['id'])
        if task is None or task['revision'] != reference['revision']:
            raise DatasetConflictError('当前分析任务已更新或删除，请重新读取详情后再发送；本轮修改未执行')
        return task

    async def prepare(self, context):
        context = dict(validate_context(context))
        context.pop('persisted_task', None)
        context.pop('user_decision', None)
        context.pop('rejected_case_ids', None)
        context.pop('environment_candidates', None)
        context.pop('confirmed_user_fields', None)
        reference = context.get('task_reference')
        # Older clients expose the same identity inside their page state.
        if not reference:
            state = context.get('state') or {}
            definition = state.get('task-definition') or {}
            plan = (state.get('execution-plan') or {}).get('task') or {}
            if context['page'] == 'task-definition' and definition.get('task_id') and definition.get('revision'):
                reference = {'id': definition['task_id'], 'revision': definition['revision']}
            elif context['page'] == 'execution-plan' and plan.get('id') and plan.get('revision'):
                reference = {'id': plan['id'], 'revision': plan['revision']}
        task = await self.read_task(reference)
        if task:
            context['task_reference'] = {'id': task['id'], 'revision': task['revision'], 'title': task['title']}
            context['persisted_task'] = {key: task.get(key) for key in ('id', 'revision', 'title', 'status', 'event_id', 'event_label', 'time_window')}
            context['persisted_task']['region'] = {key: task.get('region', {}).get(key) for key in ('mode', 'name', 'environment_id')}
            context['persisted_task']['forecast_setup'] = task.get('components', {}).get('forecast_setup')
        context['server_rules'] = {
            'forecast_numeric_bounds': {key: list(bounds) for key, bounds in BOUNDS.items()},
            'forecast_cross_constraints': ['预警密度严格小于拥挤密度上限', '模拟通道最多 12 个（A–L 编号），WGS84 坐标互异；空对象仅兼容旧版三个推定点', '来流比例按通道编号排序、数量与坐标一致、每项 0–1 且合计为 1；通道名称仅对应已有编号且不超过100字', '到达曲线最多200点，每点仅minute/rate，从0分钟开始、严格递增且位于任务时间窗内，rate为0–3000人/分钟；arrival_interpolation为smooth（默认保形三次平滑曲线）或step（阶梯）；末点后恒定，新增到达按实际曲线积分，不含初始人数', '模拟编号不是官方出口编号；园门、建筑入口、步道连接、边界交点必须区分，不能从不完整地图推断真实出口总数'],
            'persistence': 'changes 修改详情草稿；只有明确调用保存/确认操作后才会写入分析任务。执行结果以回执为准。',
        }
        validate_context(context)
        return context, task

    @staticmethod
    def history(messages):
        result, remaining = [], 18000
        for message in reversed(messages[-24:]):
            text = message['content']
            if message.get('decision'):
                text += '\n用户按钮选择：' + json.dumps(message['decision'], ensure_ascii=False)
            if message.get('plan', {}).get('confirmations'):
                text += '\n待确认建议：' + json.dumps(message['plan']['confirmations'], ensure_ascii=False)
            if message.get('plan', {}).get('input_requests'):
                text += '\n待补充事项：' + json.dumps(message['plan']['input_requests'], ensure_ascii=False)
            if message.get('execution') is not None:
                text += '\n实际页面执行结果：' + json.dumps(message['execution'], ensure_ascii=False)
            elif message.get('plan', {}).get('changes') or message.get('plan', {}).get('action'):
                text += '\n该轮没有执行回执，不能认为页面修改已成功。'
            entry = '[' + str(message.get('page_title') or message.get('page')) + '] ' + text[:8000]
            if len(entry) > remaining:
                break
            result.insert(0, {'role': message['role'], 'content': entry})
            remaining -= len(entry)
        return result

    async def generate(self, history, content, context, provider, decision=None):
        plan = await self._generate(history, content, context, provider, decision)
        if context.get('page') == 'task-definition':
            plan = explain_model_proposals(limit_requirement_cards(plan), context)
            return finish_requirement_turn(plan, context, content)
        return limit_requirement_cards(plan)

    async def _generate(self, history, content, context, provider, decision=None):
        context, task = await self.prepare(context)
        context['user_decision'] = decision
        context['current_request'] = content
        if context['page'] == 'task-definition' and decision:
            if decision.get('task_binding') is not None and decision['task_binding'] != task_binding(context):
                raise DatasetConflictError('任务已切换，原确认项未应用，请在当前任务中重新提出建议')
            if decision['kind'] == 'environment':
                plan = await asyncio.to_thread(self.environment_skill.resolve, decision, context, task)
                await self.read_task(context.get('task_reference'))
                return await advance_requirements(plan, context, history, task, self.case_search)
            plan = resolve_proposal(decision, context, task)
            if decision['kind'] == 'case':
                raw = apply_case_decision({'reply': f'已按你的选择处理“{decision["title"]}”的借鉴范围；右侧更新情况见执行回执。'}, context)
                plan = validate_editor_operations(validate_plan(raw, context), context, task)
                validate_case_choices(plan, context)
                await self.validate_references(plan, context)
            await self.read_task(context.get('task_reference'))
            return await advance_requirements(plan, context, history, task, self.case_search)
        skill_plan = defer_input_skill(content, context)
        if skill_plan is not None:
            return await advance_requirements(skill_plan, context, history, task, self.case_search)
        skill_plan = experiment_parameter_skill(content, context)
        if skill_plan is not None:
            # A parameter-only edit is complete; do not cue unrelated inputs.
            return skill_plan
        skill_plan = time_window_skill(content, context)
        if skill_plan is not None:
            return await advance_requirements(skill_plan, context, history, task, self.case_search)
        if context['page'] == 'task-definition' and asks_to_continue(content):
            next_plan = validate_plan({'reply': '继续核对下一项。'}, context)
            next_plan = await advance_requirements(next_plan, context, history, task, self.case_search)
            if next_plan['confirmations'] or next_plan['input_requests'] or next_plan['reply'] != '继续核对下一项。':
                return next_plan
        choices = {}
        for message in history:
            review = message.get('decision') or {}
            if review.get('kind') == 'case':
                choices[review['subject_id']] = review['choice']
        if decision and decision['kind'] == 'case':
            choices[decision['subject_id']] = decision['choice']
        context['rejected_case_ids'] = [key for key, value in choices.items() if value == 'reject']
        if context['page'] == 'task-definition':
            context['environment_candidates'] = await asyncio.to_thread(self.environment_skill.search, content, context, history)
            context['confirmed_user_fields'] = grounded_requirement_changes(content, context)
            context['requirement_brief'] = requirement_brief(context)
            # Use current draft fields and the request, not only the saved task.
            fields = {row['id']: row.get('value') for row in context['fields']}
            query = ' '.join(str(fields.get(key) or '') for key in ('task.title', 'requirement.analysis_goal', 'requirement.key_questions')) + ' ' + content
            if needs_case_search(content, context):
                context['retrieved_cases'] = await asyncio.to_thread(self.case_search.search, query[:12000], task)
            context['requirement_dialogue'] = requirement_dialogue(context, history, task)
            prompt = load_prompt('requirement_assistant') + '\n\n' + load_prompt('requirement_consultation')
        else:
            prompt = load_prompt('page_assistant')
        contract = {
            'editable_fields': [{key: row[key] for key in ('id', 'schema', 'constraints') if key in row}
                                for row in context['fields'] if row.get('enabled', True)],
            'available_actions': [{key: row[key] for key in ('id', 'schema') if key in row}
                                  for row in context['actions'] if row.get('enabled', True)],
            'input_targets': context.get('input_targets', []),
        }
        if context['page'] == 'task-definition':
            contract['input_requests_schema'] = input_request_contract(context)
        messages = [{'role': 'system', 'content': prompt + '\n本轮唯一可写字段和操作契约（优先于旧对话中的字段名）：\n' + json.dumps(contract, ensure_ascii=False)}, *self.history(history),
                    {'role': 'user', 'content': json.dumps({'context': context, 'request': content}, ensure_ascii=False)}]
        try:
            plan = await asyncio.wait_for(self.generate_plan(messages, context, task, provider), timeout=180)
            if context['page'] == 'task-definition':
                self.environment_skill.attach(plan, context)
            if context.get('retrieved_cases') is not None:
                plan['retrieval'] = context['retrieved_cases']
            return await advance_requirements(plan, context, history, task, self.case_search)
        except asyncio.TimeoutError:
            raise ModelError('模型响应超时，详情未修改，请稍后重试或切换可用模型') from None

    async def generate_plan(self, messages, context, task, provider):
        # One regeneration for truncation and one format repair, all inside the
        # existing overall timeout. No partial answer ever reaches page editors.
        max_tokens, length_retries, format_repairs = 8192, 0, 0
        while True:
            response = await self.gateway.chat(provider, {'messages': messages, 'temperature': .1, 'max_tokens': max_tokens, 'response_format': {'type': 'json_object'}}, concise_json=True)
            choices = response.get('choices') if isinstance(response, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ModelError('模型未返回有效的助手响应')
            choice = choices[0]
            if choice.get('finish_reason') == 'length':
                logger.warning('Page assistant output truncated: provider=%s max_tokens=%s completion_tokens=%s',
                               provider, max_tokens, (response.get('usage') or {}).get('completion_tokens'))
                if length_retries:
                    raise ModelError('所选模型的输出额度仍不足，已自动重试；详情未修改。请分次调整较长内容，或切换模型后重试')
                length_retries += 1
                max_tokens = 16384
                # Regenerate a complete plan from the same request; never splice
                # incomplete JSON or include an unfinished edit as chat history.
                continue
            message = choice.get('message')
            if not isinstance(message, dict) or not isinstance(message.get('content'), str):
                raise ModelError('模型未返回文本，请检查所选模型的对话接口')
            text = message['content'].strip()
            if text.startswith('```'):
                text = text.partition('\n')[2].rsplit('```', 1)[0].strip()
            try:
                raw = json.loads(text)
                if context['page'] == 'task-definition' and isinstance(raw, dict):
                    raw = apply_case_decision(raw, context)
                    raw['changes'] = normalize_editor_changes(raw.get('changes', {}), context)
                    raw['changes'].update(context.get('confirmed_user_fields', {}))
                plan = validate_editor_operations(validate_plan(raw, context), context, task)
                if context['page'] == 'task-definition':
                    validate_case_choices(plan, context)
                    validate_requirement_interactions(plan, context, task, context['current_request'])
                if 'task.environment' in plan['changes'] and await asyncio.to_thread(self.tasks.environments.get, plan['changes']['task.environment']) is None:
                    raise ValueError('所选环境库区域已不存在，请使用当前有效区域')
                await self.validate_references(plan, context)
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning('Page assistant validation failed: page=%s provider=%s repair=%s reason=%s',
                               context['page'], provider, format_repairs, exc)
                if format_repairs:
                    raise ModelError('助手未返回有效的页面修改：' + str(exc)) from None
                format_repairs += 1
                writable = [row['id'] for row in context['fields'] if row.get('enabled', True)]
                messages.extend([{'role': 'assistant', 'content': text[:16000]}, {'role': 'user', 'content': '刚才的 JSON 或页面修改未通过校验，尚未执行。请严格依据原始请求、fields、actions 和 server_rules 修正，仅输出合法 JSON；不要扩大修改范围。校验原因：' + str(exc) + '\n可写字段 ID：' + json.dumps(writable, ensure_ascii=False) + '\nchanges 必须为平铺的字段 ID → 完整值字典。state/form/components 中的名称不是字段 ID。requirement.plan 的子项只能放在该完整对象内部。保留用户实际要求的修改；不能用空 changes 假装已完成。'}])
                continue
            await self.read_task(context.get('task_reference'))
            return plan

    async def validate_references(self, plan, context):
        for case_id in plan['changes'].get('task.references', []):
            example_state = (context.get('state') or {}).get('task-definition') or {}
            example_ids = {row['id'] for row in example_state.get('visible_case_candidates', []) + example_state.get('references', []) + example_state.get('example_case_catalog', [])}
            if context.get('example_mode') and case_id in example_ids:
                continue
            if await asyncio.to_thread(self.case_search.cases.get, case_id) is None:
                raise ValueError('引用的案例已不存在，请重新检索')
