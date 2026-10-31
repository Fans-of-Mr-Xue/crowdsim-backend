"""Integration checks for deterministic time editing and live environment matching."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from data_service.repositories.datasets import DatasetConflictError
from data_service.repositories.page_assistant import PageAssistantRepository
from data_service.schemas.assistant_confirmations import resolve_decision
from data_service.schemas.page_assistant import validate_plan
from data_service.services.page_assistant import PageAssistantService
from data_service.services.requirement_interactions import validate_requirement_interactions
from data_service.services.requirement_skills import time_window_skill

ENV = 'shanghai-monument-portrait-region'
LOCATION = '上海人民英雄纪念塔附近'
REQUEST = '我想研究跨年期间上海人民英雄纪念塔附近的聚集风险，先帮我明确需要研究什么。'


def context(location=''):
    return {'page': 'task-definition', 'fields': [
        {'id': 'task.location', 'value': location, 'schema': {'type': 'string'}},
        {'id': 'task.title', 'value': '跨年研究', 'schema': {'type': 'string'}},
        {'id': 'task.timeWindow', 'value': '2026-12-31 20:00 至 2027-01-01 02:00', 'schema': {'type': 'string'}},
        {'id': 'task.environment', 'value': '', 'schema': {'type': 'string', 'enum': [ENV, 'other-env']}},
    ], 'actions': [], 'input_targets': [{'id': 'scope'}]}


class RequirementSkillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        enable_gridfs_integration()
        self.db = mongomock.MongoClient().crowdsim
        self.gateway = Mock(chat=AsyncMock())
        self.service = PageAssistantService(self.db, self.gateway)
        profiles = json.loads((Path(__file__).resolve().parents[1] / 'data_service/resources/environment_profiles.json').read_text())
        seed = next(row for row in profiles if row['id'] == ENV)
        self.profile = {**seed, '_id': seed['id'], 'revision': 1, 'is_deleted': False}
        self.profile.pop('id')
        self.db.environment_profiles.insert_one(self.profile)
        self.gateway.chat.return_value = {'choices': [{'message': {'content': json.dumps({
            'reply': '建议研究人数变化与拥挤风险，环境范围请在下方确认。', 'changes': {'task.location': LOCATION}})}}]}

    async def test_modify_time_window_opens_widget_without_model_and_without_guessing_values(self):
        result = await self.service.generate([], '我要修改时间窗', context(LOCATION), 'stub')
        self.assertEqual(result['input_requests'][0]['widget'], 'time-window')
        self.assertEqual(result['changes'], {})
        self.assertIsNone(result['action'])
        self.gateway.chat.assert_not_called()
        for request in ('不要修改时间窗', '修改时间窗为2027-01-01 00:00至2027-01-01 03:00',
                        '时间窗延长两小时', '我要修改时间窗，并更换研究地点'):
            self.assertIsNone(time_window_skill(request, context()))

    async def test_user_reported_chinese_time_sets_field_and_computes_three_hours(self):
        result = await self.service.generate([], '时间选择为2026年12月31日晚10点至2027年1月1日凌晨1点吧', context(LOCATION), 'stub')
        self.assertEqual(result['changes'], {'task.timeWindow': '2026-12-31 22:00 至 2027-01-01 01:00'})
        self.assertIn('共3小时', result['reply'])
        self.assertNotIn('场景条件', result['reply'])
        self.assertEqual(result['input_requests'][0]['title'], '确认研究区域')
        self.assertIn('下一步：', result['reply'])
        self.gateway.chat.assert_not_called()

    async def test_new_conversations_fill_explicit_facts_even_when_model_omits_changes(self):
        # Two independently created empty chats, not a repair of a historical ID.
        self.gateway.chat.return_value = {'choices': [{'message': {'content': json.dumps({
            'reply': '请确认研究范围并补充时间。', 'changes': {}})}}]}
        for topic in ('聚集风险', '人群规模变化'):
            with self.subTest(topic=topic):
                ctx = context()
                for field in ctx['fields']:
                    field['value'] = ''
                ctx['fields'].append({'id': 'requirement.analysis_goal', 'value': '', 'schema': {'type': 'string'}})
                request = f'我想研究跨年期间上海人民英雄纪念塔附近的{topic}，先帮我明确需要研究什么。'
                result = await self.service.generate([], request, ctx, 'stub')
                self.assertEqual(result['changes']['task.title'], f'跨年期间上海人民英雄纪念塔附近的{topic}')
                self.assertEqual(result['changes']['requirement.analysis_goal'], f'研究跨年期间上海人民英雄纪念塔附近的{topic}')
                self.assertEqual(result['changes']['task.location'], LOCATION)
                self.assertNotIn('task.environment', result['changes'])
                self.assertNotIn('task.timeWindow', result['changes'])
                self.assertEqual(result['confirmations'][0]['kind'], 'environment')
                self.assertEqual(result['input_requests'][0]['widget'], 'time-window')

    async def test_first_turn_can_offer_environments_goal_and_time_together(self):
        another = {**deepcopy(self.profile), '_id': 'other-env', 'title': '人民英雄纪念塔周边研究范围'}
        self.db.environment_profiles.insert_one(another)
        ctx = context()
        ctx['fields'][2]['value'] = ''
        self.gateway.chat.return_value = {'choices': [{'message': {'content': json.dumps({
            'reply': '请核对区域和时间，也可以确认名称。', 'changes': {'task.location': LOCATION},
            'confirmations': [{'kind': 'proposal', 'subject_id': 'name', 'title': '研究名称', 'detail': '名称改为聚集研究', 'changes': {'task.title': '聚集研究'}}]})}}]}
        result = await self.service.generate([], REQUEST, ctx, 'stub')
        self.assertEqual(len(result['confirmations']), 2)
        self.assertEqual(result['input_requests'][0]['widget'], 'time-window')
        self.assertEqual(len(result['confirmations']) + len(result['input_requests']), 3)
        self.gateway.chat.assert_awaited_once()

    async def test_goal_expansion_is_bound_to_recorded_user_goal_until_accepted(self):
        ctx = context()
        ctx['fields'].append({'id': 'requirement.analysis_goal', 'value': '', 'schema': {'type': 'string'}})
        expanded = '研究人数随时间的变化，并比较不同模型的聚集位置预测。'
        self.gateway.chat.return_value = {'choices': [{'message': {'content': json.dumps({
            'reply': '已记录原始研究目标，补充建议请确认。', 'changes': {},
            'confirmations': [{'kind': 'proposal', 'subject_id': 'goal', 'title': '补充研究目标', 'detail': expanded,
                               'changes': {'requirement.analysis_goal': expanded}}]})}}]}
        result = await self.service.generate([], REQUEST, ctx, 'stub')
        original = '研究跨年期间上海人民英雄纪念塔附近的聚集风险'
        self.assertEqual(result['changes']['requirement.analysis_goal'], original)
        card = result['confirmations'][0]
        self.assertEqual(card['base_values']['requirement.analysis_goal'], original)
        self.assertEqual(card['changes']['requirement.analysis_goal'], expanded)
        for field in ctx['fields']:
            field['value'] = result['changes'].get(field['id'], field['value'])
        accepted = await self.service.generate([], '同意', ctx, 'stub', {**card, 'choice': 'accept'})
        self.assertEqual(accepted['changes']['requirement.analysis_goal'], expanded)
        self.gateway.chat.assert_awaited_once()

    async def test_calendar_submission_updates_exact_time_without_model(self):
        content = '关于“修改研究时间窗”的补充：\n2026-12-31 20:00 至 2027-01-01 02:00'
        result = await self.service.generate([], content, context(LOCATION), 'stub')
        self.assertEqual(result['changes'], {'task.timeWindow': '2026-12-31 20:00 至 2027-01-01 02:00'})
        self.gateway.chat.assert_not_called()
        with self.assertRaises(ValueError):
            await self.service.generate([], content.replace('2027-01-01', '2026-01-01'), context(), 'stub')

    async def test_request_searches_live_library_and_offers_bounded_environment_not_auto_apply(self):
        result = await self.service.generate([], REQUEST, context(), 'stub')
        self.assertNotIn('task.environment', result['changes'])
        card = result['confirmations'][0]
        self.assertEqual(card['kind'], 'environment')
        self.assertEqual(card['subject_id'], ENV)
        self.assertNotIn('库内位置', card['detail'])
        self.assertNotIn('范围说明', card['detail'])
        self.assertIn('画像资料仍需核对', card['detail'])
        self.assertEqual(card['base_values']['task.location'], LOCATION)
        self.assertEqual(card['environment_revision'], 1)
        request_context = json.loads(self.gateway.chat.call_args.args[1]['messages'][-1]['content'])['context']
        self.assertEqual(request_context['environment_candidates'][0]['id'], ENV)

    async def test_accept_uses_saved_card_and_reject_does_not_modify_or_call_model(self):
        plan = await self.service.generate([], REQUEST, context(), 'stub')
        repository = PageAssistantRepository(self.db)
        thread = repository.create_completed('a' * 32, REQUEST, context(), plan, 'stub')
        message = repository.get('a' * 32, thread['id'])['messages'][-1]
        for choice in ('accept', 'reject'):
            decision = resolve_decision([message], {'message_id': message['id'], 'confirmation_id': message['plan']['confirmations'][0]['id'], 'choice': choice})
            self.gateway.chat.reset_mock()
            result = await self.service.generate([], '按钮选择', context(LOCATION), 'stub', decision)
            self.assertEqual(result['changes'], {'task.environment': ENV} if choice == 'accept' else {})
            if choice == 'accept':
                self.assertEqual(result['environment_selection'], {'id': ENV, 'revision': 1})
            self.assertIsNone(result['action'])
            self.gateway.chat.assert_not_called()

    async def test_custom_choice_opens_existing_map_action_without_adopting_candidate(self):
        plan = await self.service.generate([], REQUEST, context(), 'stub')
        message = {'id': 'message', 'role': 'assistant', 'plan': plan}
        decision = resolve_decision([message], {'message_id': 'message', 'confirmation_id': plan['confirmations'][0]['id'], 'choice': 'customize'})
        ctx = context(LOCATION)
        ctx['actions'] = [{'id': 'task.draw-boundary', 'schema': {'type': 'object', 'properties': {}}}]
        self.gateway.chat.reset_mock()
        result = await self.service.generate([], '自己圈定范围', ctx, 'stub', decision)
        self.assertEqual(result['changes'], {})
        self.assertEqual(result['action'], {'id': 'task.draw-boundary', 'parameters': {}})
        self.assertEqual(result['environment_preview'], {'id': ENV, 'revision': 1})
        self.gateway.chat.assert_not_called()
        fake = deepcopy(message)
        fake['plan']['confirmations'][0]['kind'] = 'proposal'
        with self.assertRaisesRegex(ValueError, '仅用于环境匹配'):
            resolve_decision([fake], {'message_id': 'message', 'confirmation_id': plan['confirmations'][0]['id'], 'choice': 'customize'})

    async def test_changed_location_or_environment_revision_blocks_old_acceptance(self):
        plan = await self.service.generate([], REQUEST, context(), 'stub')
        decision = {**plan['confirmations'][0], 'choice': 'accept'}
        with self.assertRaises(DatasetConflictError):
            await self.service.generate([], '同意', context('其他地点'), 'stub', decision)
        self.db.environment_profiles.update_one({'_id': ENV}, {'$inc': {'revision': 1}})
        with self.assertRaises(DatasetConflictError):
            await self.service.generate([], '同意', context(LOCATION), 'stub', decision)
        self.db.environment_profiles.update_one({'_id': ENV}, {'$set': {'is_deleted': True}})
        with self.assertRaises(DatasetConflictError):
            await self.service.generate([], '同意', context(LOCATION), 'stub', decision)

    async def test_pending_and_rejected_offer_not_repeated_but_explicit_research_can_repeat(self):
        plan = await self.service.generate([], REQUEST, context(), 'stub')
        history = [{'role': 'assistant', 'content': plan['reply'], 'plan': plan}]
        self.assertEqual(self.service.environment_skill.search('补充时间', context(LOCATION), history), [])
        history.append({'role': 'user', 'content': '不同意', 'decision': {**plan['confirmations'][0], 'choice': 'reject'}})
        self.assertEqual(self.service.environment_skill.search('补充时间', context(LOCATION), history), [])
        self.assertEqual(self.service.environment_skill.search('重新检索环境', context(LOCATION), history)[0]['id'], ENV)

    async def test_missing_deleted_or_already_selected_environment_not_offered(self):
        self.assertEqual(self.service.environment_skill.search('研究北京某广场', context(), []), [])
        selected = context(LOCATION)
        selected['fields'][-1]['value'] = ENV
        self.assertEqual(self.service.environment_skill.search(REQUEST, selected, []), [])
        self.db.environment_profiles.update_one({'_id': ENV}, {'$set': {'is_deleted': True}})
        self.assertEqual(self.service.environment_skill.search(REQUEST, context(), []), [])

    async def test_user_created_matches_are_real_candidates_and_not_silently_selected(self):
        another = {**deepcopy(self.profile), '_id': 'other-env', 'title': '人民英雄纪念塔周边研究范围'}
        self.db.environment_profiles.insert_one(another)
        result = self.service.environment_skill.search(REQUEST, context(), [])
        self.assertEqual({item['id'] for item in result}, {ENV, 'other-env'})

    async def test_environment_cannot_be_selected_by_model_without_confirmation(self):
        ctx = context(LOCATION)
        plan = validate_plan({'reply': '已选定', 'changes': {'task.environment': ENV}}, ctx)
        with self.assertRaisesRegex(ValueError, '环境确认卡片'):
            validate_requirement_interactions(plan, ctx, None, REQUEST)


if __name__ == '__main__':
    unittest.main()
