"""Follow real edit/decision paths through case, method and input guidance."""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, Mock

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from data_service.schemas.assistant_confirmations import resolve_decision
from data_service.schemas.research_plan import PLAN_SCHEMA
from data_service.services.page_assistant import PageAssistantService


def context():
    values = {
        'task.title': '跨年聚集风险', 'task.location': '上海人民英雄纪念塔附近',
        'task.timeWindow': '', 'task.environment': 'monument',
        'requirement.analysis_goal': '研究什么时间、哪些出口容易拥堵',
        'requirement.key_questions': '什么时间节点，什么出口最容易堵塞',
        'requirement.expected_outputs': ['density', 'bottlenecks', 'exits'],
        'task.references': [], 'task.reference_notes': [],
        'requirement.plan': dict(objective='研究什么时间、哪些出口容易拥堵', indicators=[], data_needs=[], steps=[], assumptions=[], open_questions=[]),
    }
    schemas = {key: {'type': 'string'} for key in values}
    schemas.update({'task.references': {'type': 'array', 'items': {'type': 'string'}}, 'task.reference_notes': {'type': 'array'},
        'requirement.expected_outputs': {'type': 'array', 'items': {'type': 'string', 'enum': ['density', 'bottlenecks', 'exits', 'comparison']}},
        'requirement.plan': PLAN_SCHEMA})
    return {'page': 'task-definition', 'fields': [{'id': key, 'value': value, 'schema': schemas[key]} for key, value in values.items()],
            'actions': [], 'input_targets': [{'id': key} for key in ('scope', 'goal', 'outputs', 'approach', 'inputs', 'cases')]}


def apply(ctx, plan):
    for field in ctx['fields']:
        if field['id'] in plan['changes']:
            field['value'] = deepcopy(plan['changes'][field['id']])


def message(plan, identifier):
    return {'role': 'assistant', 'page': 'task-definition', 'id': identifier, 'content': plan['reply'], 'plan': plan, 'execution': {'status': 'applied'}}


class RequirementProgressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        enable_gridfs_integration()
        self.db = mongomock.MongoClient().crowdsim
        self.gateway = Mock(chat=AsyncMock(return_value={'choices': [{'message': {'content': json.dumps({'reply': '已记录。', 'changes': {}})}}]}))
        self.service = PageAssistantService(self.db, self.gateway)
        self.db.case_profiles.insert_one({'_id': 'bund-case', 'name': '上海跨年人流案例', 'summary': '跨年人流在狭窄台阶处发生拥堵，需关注分时到达。', 'topic': '聚集风险', 'revision': 1, 'is_deleted': False})

    async def test_time_completion_then_case_then_models_then_one_input(self):
        ctx = context()
        first = await self.service.generate([], '2026-12-31 18:00 至 2027-01-01 02:00', ctx, 'stub')
        self.assertIn('task.timeWindow', first['changes'])
        self.assertEqual([c['kind'] for c in first['confirmations']], ['case'])
        self.assertEqual(first['input_requests'], [])
        self.assertNotIn('task.references', first['changes'])
        self.assertNotIn('管控情景', first['confirmations'][0]['detail'])
        apply(ctx, first)
        history = [message(first, 'first')]
        decision = resolve_decision(history, {'message_id': 'first', 'confirmation_id': first['confirmations'][0]['id'], 'choice': 'accept'})
        second = await self.service.generate(history, '按钮同意', ctx, 'stub', decision)
        self.assertEqual(second['changes']['task.references'], ['bund-case'])
        self.assertNotIn('requirement.plan', second['changes'])
        methods = second['confirmations'][0]
        self.assertEqual(methods['title'], '确认比较模型')
        self.assertNotIn('Weidmann', second['reply'])
        for name in ('分区流量守恒', 'CFS 无碰撞速度模型', '社会力模型 SFM'):
            self.assertIn(name, second['reply'])
        self.assertIn('需要输入', second['reply'])
        self.assertIn('https://doi.org/', second['reply'])
        self.assertEqual(second['input_requests'], [])
        apply(ctx, second)
        history += [{'role': 'user', 'content': '同意案例', 'decision': decision}, message(second, 'second')]
        decision = resolve_decision(history, {'message_id': 'second', 'confirmation_id': methods['id'], 'choice': 'accept'})
        third = await self.service.generate(history, '按钮同意', ctx, 'stub', decision)
        research = third['changes']['requirement.plan']
        self.assertEqual(len(research['steps']), 3)
        self.assertEqual(research['data_needs'][0]['status'], 'available')
        self.assertEqual(len(research['experiment']['model_ids']), 3)
        self.assertNotIn('随机种子', [row['name'] for row in research['data_needs']])
        self.assertEqual(len(third['input_requests']), 1)
        self.assertEqual(third['input_requests'][0]['input_name'], '步道宽度与可行走面')
        self.assertIsNone(third['action'])
        self.gateway.chat.assert_not_called()
        apply(ctx, third)
        research['data_needs'][1].update(status='available', source='user', detail='用户核对了已有步道几何与宽度。')
        apply(ctx, {'changes': {'requirement.plan': research}})
        history += [{'role': 'user', 'content': '同意模型', 'decision': decision}, message(third, 'third')]
        fourth = await self.service.generate(history, '继续', ctx, 'stub')
        self.assertEqual(fourth['input_requests'][0]['input_name'], '障碍物与不可通行区域')

    async def test_environment_confirmation_as_last_missing_field_also_advances(self):
        ctx = context()
        apply(ctx, {'changes': {'task.environment': '', 'task.timeWindow': '2026-12-31 18:00 至 2027-01-01 02:00'}})
        self.service.environment_skill.resolve = Mock(return_value={'reply': '已提交区域选择。', 'changes': {'task.environment': 'monument'}, 'action': None, 'confirmations': [], 'input_requests': []})
        plan = await self.service.generate([], '使用此区域', ctx, 'stub', {'kind': 'environment', 'choice': 'accept'})
        self.assertEqual(plan['confirmations'][0]['kind'], 'case')
        self.gateway.chat.assert_not_called()

    async def test_continue_does_not_reask_known_outputs_or_stop_at_generic_offer(self):
        ctx = context()
        apply(ctx, {'changes': {'task.timeWindow': '2026-12-31 18:00 至 2027-01-01 02:00'}})
        first = await self.service.generate([], '嗯，可以。继续', ctx, 'stub')
        self.assertEqual(first['confirmations'][0]['kind'], 'case')
        self.assertFalse(any(c.get('subject_id') == 'progress-outputs' for c in first['confirmations']))
        self.gateway.chat.assert_not_called()

    async def test_accept_outputs_offers_methods_and_rejecting_methods_asks_for_adjustment(self):
        self.db.case_profiles.delete_many({})
        ctx = context()
        apply(ctx, {'changes': {'requirement.expected_outputs': []}})
        first = await self.service.generate([], '2026-12-31 18:00 至 2027-01-01 02:00', ctx, 'stub')
        apply(ctx, first)
        history = [message(first, 'first')]
        decision = resolve_decision(history, {'message_id': 'first', 'confirmation_id': first['confirmations'][0]['id'], 'choice': 'accept'})
        second = await self.service.generate(history, '同意', ctx, 'stub', decision)
        self.assertEqual(second['confirmations'][0]['subject_id'], 'progress-methods')
        apply(ctx, second)
        history += [{'role': 'user', 'content': '同意输出', 'decision': decision}, message(second, 'second')]
        decision = resolve_decision(history, {'message_id': 'second', 'confirmation_id': second['confirmations'][0]['id'], 'choice': 'reject'})
        third = await self.service.generate(history, '不同意', ctx, 'stub', decision)
        self.assertEqual(third['confirmations'], [])
        self.assertEqual(third['changes'], {})
        self.assertEqual(third['input_requests'][0]['title'], '调整模型比较思路')

    async def test_explicit_pause_and_unfinished_scope_do_not_start_case_flow(self):
        ctx = context()
        self.service.case_search.search = Mock()
        plan = await self.service.generate([], '只修改时间，不要继续', ctx, 'stub')
        self.assertEqual(plan['confirmations'], [])
        apply(ctx, {'changes': {'task.environment': ''}})
        plan = await self.service.generate([], '2026-12-31 18:00 至 2027-01-01 02:00', ctx, 'stub')
        self.assertEqual(plan['confirmations'], [])
        self.service.case_search.search.assert_not_called()

    async def test_no_matching_case_moves_on_without_fabricating_one(self):
        self.db.case_profiles.delete_many({})
        ctx = context()
        apply(ctx, {'changes': {'requirement.expected_outputs': []}})
        plan = await self.service.generate([], '2026-12-31 18:00 至 2027-01-01 02:00', ctx, 'stub')
        self.assertEqual(plan['retrieval']['items'], [])
        self.assertEqual(plan['confirmations'][0]['subject_id'], 'progress-outputs')
        self.assertEqual(plan['changes'], {'task.timeWindow': '2026-12-31 18:00 至 2027-01-01 02:00'})

    async def test_pending_or_rejected_cases_are_not_repeated(self):
        ctx = context()
        first = await self.service.generate([], '2026-12-31 18:00 至 2027-01-01 02:00', ctx, 'stub')
        apply(ctx, first)
        history = [message(first, 'first')]
        pending = await self.service.generate(history, '继续', ctx, 'stub')
        self.assertEqual(pending['confirmations'], [])
        self.assertIn('先确认上方', pending['reply'])
        decision = resolve_decision(history, {'message_id': 'first', 'confirmation_id': first['confirmations'][0]['id'], 'choice': 'reject'})
        rejected = await self.service.generate(history, '不同意', ctx, 'stub', decision)
        self.assertEqual(rejected['confirmations'][0]['subject_id'], 'progress-methods')
        self.assertFalse(any(c['kind'] == 'case' for c in rejected['confirmations']))

    def prepared_context(self):
        ctx = context()
        apply(ctx, {'changes': {'task.timeWindow': '2026-12-31 20:00 至 2027-01-01 02:00'}})
        self.db.case_profiles.delete_many({})
        return ctx

    async def test_output_only_llm_reply_gets_a_bound_model_card_without_user_asking_again(self):
        ctx = self.prepared_context()
        self.gateway.chat.return_value = {'choices': [{'message': {'content': json.dumps({
            'reply': '预期输出确定为人数与密度、拥堵位置与时段，后续需要可随时补充。',
            'changes': {'requirement.expected_outputs': ['density', 'bottlenecks']}})}}]}
        result = await self.service.generate([], '对，我想看人数与密度变化、拥堵位置与关键时段', ctx, 'stub')
        card = result['confirmations'][0]
        self.assertEqual(card['subject_id'], 'progress-methods')
        self.assertEqual(card['base_values']['requirement.plan'], result['changes']['requirement.plan'])
        self.assertEqual(len(card['changes']['requirement.plan']['experiment']['model_ids']), 3)
        self.assertNotIn('experiment', result['changes']['requirement.plan'])
        self.assertIn('下一步：请在下方确认卡', result['reply'])
        apply(ctx, result)
        history = [message(result, 'outputs')]
        decision = resolve_decision(history, {'message_id':'outputs','confirmation_id':card['id'],'choice':'accept'})
        accepted = await self.service.generate(history, '同意', ctx, 'stub', decision)
        self.assertEqual(len(accepted['changes']['requirement.plan']['experiment']['model_ids']), 3)
        self.assertTrue(accepted['input_requests'])
        self.assertIn('下一步：', accepted['reply'])

    async def test_next_step_variants_work_without_llm_and_named_boundary_is_enough(self):
        ctx = self.prepared_context()
        apply(ctx, {'changes': {'task.location': '', 'task.environment': ''}})
        ctx['state'] = {'task-definition': {'region': {'name':'黄浦公园', 'geometry':{'type':'Polygon','coordinates':[[[121,31],[121.1,31],[121,31.1],[121,31]]]}}}}
        for question in ('下一步干啥？','下一步做什么','接下来怎么办','嗯，可以，继续'):
            result = await self.service.generate([], question, ctx, 'stub')
            self.assertEqual(result['confirmations'][0]['subject_id'], 'progress-methods')
            self.assertTrue(result['reply'].split('\n\n')[-1].startswith('下一步：'))
        self.gateway.chat.assert_not_called()

    async def test_missing_time_no_longer_silently_stops_after_a_reply(self):
        ctx = context()
        result = await self.service.generate([], '我希望看瓶颈位置', ctx, 'stub')
        self.assertEqual(result['input_requests'][0]['widget'],'time-window')
        self.assertIn('下一步：', result['reply'])

    async def test_retrieved_input_requires_consent_then_moves_to_next_input(self):
        from data_service.services.requirement_progression import suggested_comparison
        ctx = self.prepared_context()
        values = {f['id']: f['value'] for f in ctx['fields']}
        research = suggested_comparison(values['requirement.plan'],values,['守恒','CFS','SFM'])
        research['data_needs'][0].update(status='retrieved',source='knowledge',detail='已选择黄浦公园边界，需核对是否用于本次研究。')
        apply(ctx, {'changes':{'requirement.plan':research}})
        result = await self.service.generate([], '继续', ctx, 'stub')
        self.assertEqual(result['changes'], {})
        card = result['confirmations'][0]
        self.assertEqual(card['changes']['requirement.plan']['data_needs'][0]['status'],'available')
        history = [message(result,'review')]
        decision = resolve_decision(history,{'message_id':'review','confirmation_id':card['id'],'choice':'accept'})
        accepted = await self.service.generate(history,'同意',ctx,'stub',decision)
        self.assertEqual(accepted['changes']['requirement.plan']['data_needs'][0]['status'],'available')
        self.assertEqual(accepted['input_requests'][0]['input_name'],'步道宽度与可行走面')
        self.assertTrue(all(row['status']=='pending' for row in accepted['changes']['requirement.plan']['data_needs'][1:]))

    async def test_defer_is_persisted_as_pending_and_final_review_does_not_claim_complete_data(self):
        from data_service.services.requirement_progression import suggested_comparison
        from data_service.services.requirement_brief import DEFERRED_NOTE
        ctx = self.prepared_context()
        values = {f['id']: f['value'] for f in ctx['fields']}
        research = suggested_comparison(values['requirement.plan'],values,['守恒','CFS','SFM'])
        research['data_needs'] = [{'name':name,'source':'unknown','status':'pending','detail':'缺少此项时无法核对人数变化。'} for name in ('初始人数','分时到达人流')]
        apply(ctx,{'changes':{'requirement.plan':research}})
        first = await self.service.generate([], '关于“补充初始人数”的补充（初始人数）：\n暂时没有这项资料，请保留待补充，先核对下一项。',ctx,'stub')
        self.assertEqual(first['changes']['requirement.plan']['data_needs'][0]['status'],'pending')
        self.assertIn(DEFERRED_NOTE,first['changes']['requirement.plan']['data_needs'][0]['detail'])
        self.assertEqual(first['input_requests'][0]['input_name'],'分时到达人流')
        apply(ctx,first)
        final = await self.service.generate([], '关于“补充分时到达人流”的补充（分时到达人流）：\n暂时没有这项资料，请保留待补充，先核对下一项。',ctx,'stub')
        self.assertEqual(final['input_requests'],[])
        self.assertIn('仍保留缺口：初始人数、分时到达人流',final['reply'])
        self.assertIn('确认需求，进入领域模型',final['reply'])
        self.assertIsNone(final['action'])
        self.gateway.chat.assert_not_called()

    async def test_ready_draft_stops_questions_but_requires_whole_task_confirmation(self):
        from data_service.services.requirement_progression import suggested_comparison
        ctx = self.prepared_context()
        values = {f['id']: f['value'] for f in ctx['fields']}
        research = suggested_comparison(values['requirement.plan'],values,['守恒','CFS','SFM'])
        for row in research['data_needs']:
            row.update(status='available',source='user',detail='用户已提供具体资料并核对。')
        apply(ctx, {'changes':{'requirement.plan':research}})
        result = await self.service.generate([], '继续', ctx,'stub')
        self.assertEqual(result['input_requests'],[])
        self.assertEqual(result['confirmations'],[])
        self.assertIsNone(result['action'])
        self.assertIn('研究安排已基本整理完毕',result['reply'])
        self.assertIn('确认需求，进入领域模型',result['reply'])

    async def test_unknown_deferral_cannot_edit_or_skip_another_input(self):
        from data_service.services.requirement_progression import defer_input_skill
        self.assertIsNone(defer_input_skill('关于“补充初始人数”的补充（不存在的资料）：\n暂时没有', context()))
