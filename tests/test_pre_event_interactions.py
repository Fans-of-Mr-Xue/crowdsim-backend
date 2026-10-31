"""Exercise real service validation and saved-card decisions without model/network IO."""
import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, Mock

import mongomock
from mongomock.gridfs import enable_gridfs_integration

from data_service.schemas.page_assistant import validate_plan
from data_service.schemas.assistant_confirmations import resolve_decision
from data_service.services.requirement_interactions import validate_requirement_interactions, resolve_proposal
from data_service.services.page_assistant import PageAssistantService
from data_service.repositories.page_assistant import PageAssistantRepository
from data_service.repositories.datasets import DatasetConflictError
from data_service.schemas.analysis_tasks import validate_task


def context():
    return {'page': 'task-definition', 'example_mode': False, 'task_reference': None,
            'fields': [{'id': 'task.title', 'value': '原任务', 'schema': {'type': 'string'}},
                       {'id': 'task.summary', 'value': '已确认活动', 'schema': {'type': 'string'}}],
            'actions': [{'id': action, 'schema': {'type': 'object', 'properties': {}}} for action in ('task.save', 'task.choose', 'task.draw-boundary')],
            'input_targets': [{'id': item} for item in ('goal', 'scope', 'inputs', 'approach')]}


def proposal(ctx=None):
    ctx = ctx or context()
    raw = {'reply': '建议名称', 'confirmations': [{'kind': 'proposal', 'subject_id': 'title', 'title': '修改名称', 'detail': '名称改为高峰聚集研究', 'changes': {'task.title': '高峰聚集研究'}}]}
    plan = validate_requirement_interactions(validate_plan(raw, ctx), ctx, None, '建议一个名称')
    plan['confirmations'][0]['id'] = 'review-one'
    return plan


def choose(plan, choice='accept'):
    return resolve_decision([{'id': 'message-one', 'role': 'assistant', 'plan': plan}], {'message_id': 'message-one', 'confirmation_id': 'review-one', 'choice': choice})


class PreEventInteractionTests(unittest.TestCase):
    def test_time_and_location_are_separate_even_when_they_share_scope(self):
        result = validate_plan({'reply': '请分别确认。', 'input_requests': [
            {'target': 'scope', 'title': '补充研究时间窗与区域', 'detail': '请提供时间，并确认环境库区域。'},
            {'target': 'scope', 'title': '补充研究地点', 'detail': '请提供地点名称。'},
        ]}, context())
        time, location = result['input_requests']
        self.assertEqual(time['title'], '补充研究时间窗')
        self.assertEqual(time['widget'], 'time-window')
        self.assertNotIn('区域', time['detail'])
        self.assertEqual(location['title'], '补充研究地点')

    def test_location_can_be_recorded_without_inventing_geometry(self):
        ctx = context()
        ctx['fields'].append({'id': 'task.location', 'value': '', 'schema': {'type': 'string', 'maxLength': 200}})
        plan = validate_requirement_interactions(validate_plan({'reply': '将记录地点', 'changes': {'task.location': '上海人民英雄纪念塔附近'}}, ctx), ctx, None, '研究纪念塔附近')
        self.assertEqual(plan['changes'], {'task.location': '上海人民英雄纪念塔附近'})
        self.assertIsNone(plan['action'])

    def test_only_conversation_drafts_can_preserve_an_unbounded_region(self):
        draft = {'title': '待明确时间的研究', 'status': 'draft', 'region': {'mode': 'custom', 'name': '', 'geometry': None},
                 'components': {'definition_ui': {'assessment_form': {'location': '上海人民英雄纪念塔附近'}}}}
        saved = validate_task(draft, allow_unbounded_draft=True)
        self.assertIsNone(saved['region']['geometry'])
        self.assertEqual(saved['components']['definition_ui']['assessment_form']['location'], '上海人民英雄纪念塔附近')
        # The normal analysis-task API and confirmation still require a real boundary.
        with self.assertRaises(ValueError):
            validate_task(draft)
        with self.assertRaises(ValueError):
            validate_task({**draft, 'status': 'confirmed'}, allow_unbounded_draft=True)
        with self.assertRaises(ValueError):
            validate_task({**draft, 'region': {'mode': 'custom', 'geometry': {'type': 'made-up'}}}, allow_unbounded_draft=True)

    def test_saved_effect_is_bound_and_only_applied_after_accept(self):
        ctx = context()
        plan = proposal(ctx)
        self.assertEqual(plan['changes'], {})
        card = plan['confirmations'][0]
        self.assertEqual(card['base_values'], {'task.title': '原任务'})
        accepted = resolve_proposal(choose(plan), ctx, None)
        self.assertEqual(accepted['changes'], card['changes'])
        self.assertIsNone(accepted['action'])
        self.assertEqual(resolve_proposal(choose(plan, 'reject'), ctx, None)['changes'], {})
        self.assertEqual(ctx['fields'][0]['value'], '原任务')

    def test_stale_task_or_related_edits_do_not_overwrite(self):
        plan = proposal()
        for mutate in (lambda ctx: ctx['fields'][0].update(value='手动改名'), lambda ctx: ctx.update(example_mode=True)):
            ctx = context()
            mutate(ctx)
            with self.assertRaises(DatasetConflictError):
                resolve_proposal(choose(plan), ctx, None)
        ctx = context()
        ctx['fields'][1]['value'] = '无关字段可继续编辑'
        self.assertEqual(resolve_proposal(choose(plan), ctx, None)['changes']['task.title'], '高峰聚集研究')

    def test_no_early_effects_unknown_fields_or_unbound_proposals(self):
        ctx = context()
        raw = {'reply': '建议', 'confirmations': [{'kind': 'proposal', 'subject_id': 'title', 'title': '名称', 'detail': '新名称', 'changes': {'task.title': '新名称'}}]}
        for changes in ({'task.title': '抢先应用'},):
            with self.assertRaisesRegex(ValueError, '提前'):
                validate_requirement_interactions(validate_plan({**raw, 'changes': changes}, ctx), ctx, None, '建议')
        for effects in ({}, {'forecast.run': True}, {'task.title': 12}):
            bad = deepcopy(raw)
            bad['confirmations'][0]['changes'] = effects
            with self.assertRaises(ValueError):
                validate_requirement_interactions(validate_plan(bad, ctx), ctx, None, '建议')

    def test_duplicate_superseded_and_forged_decisions_are_rejected(self):
        plan = proposal()
        message = {'id': 'message-one', 'role': 'assistant', 'plan': plan}
        payload = {'message_id': 'message-one', 'confirmation_id': 'review-one', 'choice': 'accept'}
        for messages, click in [([message, {'role': 'user', 'decision': payload}], payload),
                                ([message, {'role': 'assistant', 'plan': plan}], payload),
                                ([message], {**payload, 'changes': {'task.title': '注入'}})]:
            with self.assertRaises(ValueError):
                resolve_decision(messages, click)

    def test_requests_only_locate_available_sections_and_remain_non_mutating(self):
        ctx = context()
        row = {'target': 'inputs', 'title': '补充分时来流', 'detail': '用于比较两种模型的人数积累；请提供时间与人数，没有可说明。', 'input_name': '分时来流'}
        plan = validate_plan({'reply': '请补充', 'input_requests': [row]}, ctx)
        self.assertEqual(plan['input_requests'], [row])
        self.assertEqual(plan['changes'], {})
        for bad in ([{**row, 'target': 'arbitrary-selector'}], [row] * 3, [{**row, 'target': 'scope'}]):
            with self.assertRaises(ValueError):
                validate_plan({'reply': '补充', 'input_requests': bad}, ctx)
        no_ui = {**ctx, 'input_targets': []}
        with self.assertRaises(ValueError):
            validate_plan({'reply': '补充', 'input_requests': [row]}, no_ui)

    def test_empty_optional_input_name_is_omitted_without_mutating_model_output(self):
        row = {'target': 'scope', 'title': '补充研究时间窗', 'detail': '请提供跨年的年份、起止日期和时间。'}
        for empty in (None, '', '  '):
            raw = {**row, 'input_name': empty}
            plan = validate_plan({'reply': '请补充时间', 'input_requests': [raw]}, context())
            self.assertEqual(plan['input_requests'], [{**row, 'widget': 'time-window', 'detail': '请选择开始和结束日期、时间；跨年时请核对年份。'}])
            self.assertIn('input_name', raw)
            self.assertEqual(raw['detail'], row['detail'])
        # Whitespace on a meaningful name is harmless, but never coerces types.
        named = {**row, 'target': 'inputs', 'input_name': ' 分时来流 '}
        self.assertEqual(validate_plan({'reply': '补充', 'input_requests': [named]}, context())['input_requests'][0]['input_name'], '分时来流')
        for invalid in (False, 0, [], {}):
            with self.assertRaisesRegex(ValueError, r'input_requests\[0\].input_name须为字符串'):
                validate_plan({'reply': '补充', 'input_requests': [{**row, 'input_name': invalid}]}, context())

    def test_request_errors_identify_required_field_and_length_for_model_repair(self):
        row = {'target': 'scope', 'title': '补充时间', 'detail': '研究时段'}
        for key, value, diagnostic in (
            ('title', '', 'title不能为空'), ('detail', None, 'detail须为字符串'),
            ('detail', ['时间'], 'detail须为字符串'), ('title', '时' * 201, 'title最多200字，实际201字'),
            ('detail', '时' * 1201, 'detail最多1200字，实际1201字'),
        ):
            with self.assertRaisesRegex(ValueError, diagnostic):
                validate_plan({'reply': '补充', 'input_requests': [{**row, key: value}]}, context())
        disabled = {**context(), 'input_targets': [{'id': 'scope', 'enabled': False}]}
        with self.assertRaisesRegex(ValueError, 'target只能选择本页可用区域'):
            validate_plan({'reply': '补充', 'input_requests': [row]}, disabled)
        with self.assertRaisesRegex(ValueError, '重复询问同一事项'):
            validate_plan({'reply': '补充', 'input_requests': [row, {**row, 'input_name': ''}]}, context())

    def test_local_choice_and_simple_edit_cannot_advance_flow(self):
        ctx = context()
        plan = validate_plan({'reply': '已记录', 'action': {'id': 'task.save', 'parameters': {}}}, ctx)
        with self.assertRaises(ValueError):
            validate_requirement_interactions(plan, ctx, None, '把名称改一下')
        self.assertIsNotNone(validate_requirement_interactions(plan, ctx, None, '保存当前草稿')['action'])
        for request in ('不要保存', '暂不保存'):
            with self.assertRaises(ValueError):
                validate_requirement_interactions(plan, ctx, None, request)
        for action in ('task.confirm', 'task.save', 'task.draw-boundary'):
            with self.assertRaises(ValueError):
                validate_requirement_interactions({**plan, 'action': {'id': action}}, {**ctx, 'user_decision': {'kind': 'case', 'choice': 'accept'}}, None, '同意')

    def test_saved_cards_survive_repository_reload_without_copying_effect_to_user_receipt(self):
        repository = PageAssistantRepository(mongomock.MongoClient().crowdsim)
        ctx, plan = context(), proposal()
        thread = repository.create_completed('a' * 32, '建议', ctx, plan, 'stub')
        reloaded = repository.get('a' * 32, thread['id'])
        card = reloaded['messages'][-1]
        decision = resolve_decision(reloaded['messages'], {'message_id': card['id'], 'confirmation_id': 'review-one', 'choice': 'accept'})
        self.assertEqual(resolve_proposal(decision, ctx, None)['changes'], {'task.title': '高峰聚集研究'})
        user, _ = repository.messages('turn', '同意', ctx, plan, 'stub', decision)
        self.assertNotIn('changes', user['decision'])


class PreEventServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        enable_gridfs_integration()
        self.gateway = Mock()
        self.gateway.chat = AsyncMock()
        self.service = PageAssistantService(mongomock.MongoClient().crowdsim, self.gateway)
        self.service.case_search.search = Mock(return_value={'items': [], 'documents': []})

    async def test_accept_and_reject_do_not_reask_model_or_search(self):
        for choice in ('accept', 'reject'):
            result = await self.service.generate([], '按钮选择', context(), 'stub', choose(proposal(), choice))
            self.assertEqual(bool(result['changes']), choice == 'accept')
            self.assertIsNone(result['action'])
        self.gateway.chat.assert_not_called()
        self.service.case_search.search.assert_not_called()

    async def test_case_click_only_updates_its_reference_and_displayed_scope(self):
        ctx = context()
        ctx['fields'] += [
            {'id': 'task.references', 'value': ['existing'], 'schema': {'type': 'array', 'items': {'type': 'string'}}},
            {'id': 'task.reference_notes', 'value': [], 'schema': {'type': 'array'}},
        ]
        self.service.case_search.cases.get = Mock(return_value={'id': 'found'})
        decision = {'kind': 'case', 'subject_id': 'candidate', 'title': '真实案例', 'detail': '只借鉴瓶颈检查，不沿用人数', 'choice': 'accept'}
        result = await self.service.generate([], '同意', ctx, 'stub', decision)
        self.assertEqual(result['changes'], {'task.references': ['existing', 'candidate'], 'task.reference_notes': [{'id': 'candidate', 'adoption_note': decision['detail']}]})
        self.assertIsNone(result['action'])
        decision['choice'] = 'reject'
        result = await self.service.generate([], '不同意', ctx, 'stub', decision)
        self.assertEqual(result['changes'], {'task.references': ['existing']})
        self.gateway.chat.assert_not_called()

    async def test_simple_change_does_not_trigger_search_but_case_request_does(self):
        self.gateway.chat.return_value = {'choices': [{'message': {'content': json.dumps({'reply': '将修改名称', 'changes': {'task.title': '新研究'}})}, 'finish_reason': 'stop'}]}
        result = await self.service.generate([], '名称改成新研究', context(), 'stub')
        self.assertEqual(result['changes'], {'task.title': '新研究'})
        self.assertNotIn('retrieval', result)
        self.service.case_search.search.assert_not_called()
        await self.service.generate([], '检索相似案例', context(), 'stub')
        self.service.case_search.search.assert_called_once()

    async def test_new_year_request_accepts_empty_optional_metadata_without_retry(self):
        ctx = context()
        ctx['fields'].append({'id': 'task.location', 'value': '', 'schema': {'type': 'string', 'maxLength': 200}})
        for empty in (None, '', '  '):
            self.gateway.chat.reset_mock()
            raw = {'reply': '建议研究人数随时间的变化和拥挤风险，请补充时间窗。',
                   'changes': {'task.location': '上海人民英雄纪念塔附近'},
                   'input_requests': [{'target': 'scope', 'title': '补充研究时间窗',
                                       'detail': '请提供年份、起止日期及时间，暂未确定可说明。', 'input_name': empty}]}
            self.gateway.chat.return_value = {'choices': [{'message': {'content': json.dumps(raw)}}]}
            result = await self.service.generate([], '我想研究跨年期间上海人民英雄纪念塔附近的聚集风险，先帮我明确需要研究什么。', ctx, 'stub')
            self.assertEqual(result['changes']['task.location'], '上海人民英雄纪念塔附近')
            self.assertNotIn('input_name', result['input_requests'][0])
            self.assertIsNone(result['action'])
            self.gateway.chat.assert_awaited_once()
            prompt = self.gateway.chat.call_args.args[1]['messages'][0]['content']
            self.assertIn('input_requests_schema', prompt)
            self.assertIn('"maxLength": 1200', prompt)
        self.service.case_search.search.assert_not_called()

    async def test_required_card_error_gives_precise_repair_feedback(self):
        invalid = {'reply': '请补充时间', 'input_requests': [{'target': 'scope', 'title': '研究时间', 'detail': ['年份', '起止时间']}]}
        repaired = deepcopy(invalid)
        repaired['input_requests'][0]['detail'] = '请提供年份、起止日期及时间。'
        self.gateway.chat.side_effect = [
            {'choices': [{'message': {'content': json.dumps(raw)}}]} for raw in (invalid, repaired)
        ]
        result = await self.service.generate([], '研究跨年聚集风险', context(), 'stub')
        self.assertEqual(result['input_requests'], [{'target': 'scope', 'widget': 'time-window', 'title': '补充研究时间窗', 'detail': '请选择开始和结束日期、时间；跨年时请核对年份。'}])
        feedback = self.gateway.chat.call_args.args[1]['messages'][-1]['content']
        self.assertIn('input_requests[0].detail须为字符串', feedback)
        self.assertEqual(self.gateway.chat.call_count, 2)

    async def test_invalid_flow_action_is_repaired_before_delivery(self):
        self.gateway.chat.side_effect = [
            {'choices': [{'message': {'content': json.dumps({'reply': '保存', 'action': {'id': 'task.save', 'parameters': {}}})}}]},
            {'choices': [{'message': {'content': json.dumps({'reply': '将更新名称', 'changes': {'task.title': '更新'}})}}]},
        ]
        result = await self.service.generate([], '只改名称', context(), 'stub')
        self.assertIsNone(result['action'])
        self.assertEqual(self.gateway.chat.call_count, 2)


if __name__ == '__main__':
    unittest.main()
