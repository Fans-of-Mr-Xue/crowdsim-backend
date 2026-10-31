"""The consultation contract must persist into real numerical run settings."""
from copy import deepcopy
import unittest

import mongomock
from data_service.prediction.catalog import method_catalog
from data_service.prediction.consultation_catalog import consultation_catalog, experiment_defaults, model_explanation
from data_service.prediction.inputs import default_settings, normalize_settings
from data_service.repositories.analysis_tasks import AnalysisTaskRepository
from data_service.schemas.research_plan import validate_research_plan
from data_service.schemas.page_assistant import validate_editor_operations
from data_service.services.requirement_progression import explain_model_proposals
from data_service.services.requirement_skills import experiment_parameter_skill
from data_service.prediction.repetitions import repetition_seeds
from test_requirement_progression import RequirementProgressionTests, context, apply, message
from data_service.schemas.assistant_confirmations import resolve_decision


def research(settings=None):
    return {'objective': '比较人群聚集', 'indicators': [], 'data_needs': [], 'steps': [], 'assumptions': [], 'open_questions': [],
            'experiment': {'model_ids': ['conservation', 'collision_free_speed', 'social_force'], 'settings': settings or {}, 'notes': '测试假设'}}


class ModelContractTests(unittest.TestCase):
    def test_requirement_model_selection_preserves_experiment_settings(self):
        ctx = context()
        original = research({'seed': 42})
        apply(ctx, {'changes': {'requirement.plan': original}})
        changed = deepcopy(original)
        changed['experiment']['model_ids'] = ['social_force']
        plan = {'changes': {'requirement.plan': changed}, 'action': None}
        validate_editor_operations(plan, ctx, None)
        changed['experiment']['settings']['seed'] = 43
        with self.assertRaisesRegex(ValueError, '独立实验控制'):
            validate_editor_operations(plan, ctx, None)

    def test_experiment_page_validates_models_and_parameters_with_backend_bounds(self):
        from data_service.prediction.demo import demo_inputs
        task, _, settings = demo_inputs()
        ctx = {'page': 'experiment-control', 'fields': [{'id': 'forecast.' + key, 'value': value} for key, value in settings.items()]}
        plan = {'changes': {'forecast.methods': ['conservation', 'social_force'], 'forecast.repetitions': 2}, 'action': None}
        validate_editor_operations(plan, ctx, task)
        plan['changes']['forecast.repetitions'] = 6
        with self.assertRaises(ValueError):
            validate_editor_operations(plan, ctx, task)

    def test_one_catalog_drives_models_explanations_and_controls(self):
        catalog = consultation_catalog()
        self.assertEqual([row['id'] for row in catalog['methods']], research()['experiment']['model_ids'])
        self.assertEqual(catalog['methods'], method_catalog())
        for model in catalog['methods']:
            self.assertTrue(all(model.get(key) for key in ('principle', 'required_inputs', 'outputs', 'citation', 'source', 'tradeoff')))
        common = {row['id'] for row in catalog['common_inputs']}
        self.assertEqual(common, {'boundary', 'walkable', 'obstacles', 'initial', 'arrivals', 'destinations', 'dwell'})
        self.assertEqual({row['key'] for row in catalog['parameters']}, set(experiment_defaults()))
        validate_research_plan(research(experiment_defaults()))
        for invalid in ({'seed': 1.5}, {'repetitions': 1.5}, {'repetitions': 6}, {'cfs_time_gap_s': -1}, {'initial_crowd': 100}, {'warning_density_p_m2': 7, 'jam_density_p_m2': 6}):
            with self.assertRaises(ValueError):
                validate_research_plan(research(invalid))

    def test_llm_generated_proposal_also_has_body_explanation(self):
        plan = {'reply': '建议如下。', 'confirmations': [{'changes': {'requirement.plan': research()}}]}
        explain_model_proposals(plan)
        self.assertIn(model_explanation(), plan['reply'])
        once = plan['reply']
        explain_model_proposals(plan)
        self.assertEqual(plan['reply'], once)

    def test_saved_experiment_is_consumed_and_both_editors_stay_in_sync(self):
        repo = AnalysisTaskRepository(mongomock.MongoClient().db)
        payload = {'title': '实验配置往返测试', 'time_window': '2026-10-22 18:00 至 2026-10-22 18:05',
                   'region': {'mode': 'custom', 'name': '测试', 'geometry': {'type': 'Polygon', 'coordinates': [[[121.48,31.24],[121.481,31.24],[121.481,31.241],[121.48,31.24]]]}},
                   'components': {'research_plan': research({'cfs_time_gap_s': 1.7, 'seed': 23})}}
        saved = repo.save(payload)
        settings = default_settings(repo.get(saved['id']))
        self.assertEqual((settings['cfs_time_gap_s'], settings['seed']), (1.7, 23))
        settings.update(cfs_time_gap_s=2.1, seed=24)
        saved = repo.put_component(saved['id'], 'forecast_setup', settings, saved['revision'])
        self.assertEqual(saved['components']['research_plan']['experiment']['settings']['cfs_time_gap_s'], 2.1)
        changed = deepcopy(saved['components']['research_plan'])
        changed['experiment']['settings']['cfs_time_gap_s'] = 1.2
        saved = repo.put_component(saved['id'], 'research_plan', changed, saved['revision'])
        self.assertEqual(normalize_settings(saved)['cfs_time_gap_s'], 1.2)
        self.assertEqual(saved['components']['forecast_setup']['seed'], 24)

    def test_parameter_edits_do_not_repeat_the_model_lecture(self):
        plan = {'reply': '调整间距时间。', 'changes': {'requirement.plan': research({'cfs_time_gap_s': 1.2})}}
        explain_model_proposals(plan, {'fields': [{'id': 'requirement.plan', 'value': research()}]})
        self.assertEqual(plan['reply'], '调整间距时间。')

    def test_explicit_parameter_edit_preserves_the_entire_plan(self):
        ctx = context()
        original = research({'seed': 81})
        apply(ctx, {'changes': {'requirement.plan': original}})
        plan = experiment_parameter_skill('只把实验控制台的 CFS 间距时间改为 1.2 秒，其余配置、研究目标和通用输入保持原样，不要继续下一步。', ctx)
        self.assertEqual(plan['changes'], {})
        self.assertIn('实验控制', plan['reply'])
        self.assertIn('未修改参数', plan['reply'])
        self.assertEqual(original, research({'seed': 81}))
        self.assertEqual(plan['confirmations'], [])

    def test_parameter_assignments_reject_ambiguity_units_and_out_of_bounds(self):
        ctx = context()
        apply(ctx, {'changes': {'requirement.plan': research()}})
        for request in ('不要把CFS间距时间改为1.2秒', '如果把CFS间距时间改为1.2秒会怎样？',
                        'CFS间距时间改为1.2分钟', 'CFS间距时间改为1.2秒，并修改研究地点',
                        'CFS间距时间改为1.2秒，再改为2秒'):
            self.assertIsNone(experiment_parameter_skill(request, ctx), request)
        for request in ('CFS间距时间改为-1秒', '随机种子改为1.5', '密度提示阈值改为7'):
            self.assertEqual(experiment_parameter_skill(request, ctx)['changes'], {})
        plan = experiment_parameter_skill('CFS间距时间改为1.2秒，并把随机种子设为43', ctx)
        self.assertEqual(plan['changes'], {})
        repeated = experiment_parameter_skill('重复次数改为3次', ctx)
        self.assertEqual(repeated['changes'], {})

    def test_repeat_seed_bounds_and_draft_persistence(self):
        self.assertEqual(repetition_seeds({'seed': 2147483647, 'repetitions': 3}), [2147483647, 0, 1])
        for value in (0, 6, 1.5, True):
            with self.assertRaises(ValueError):
                repetition_seeds({'seed': 42, 'repetitions': value})
        validate_research_plan(research({'repetitions': 5}))

    def test_old_forecast_observations_are_not_overwritten_by_catalog_defaults(self):
        task = {'time_window': '2026-10-22 18:00 至 2026-10-22 18:05', 'components': {'forecast_parameters': {'initialState': {'speed': 1.1}, 'densityThreshold': 3}}}
        self.assertEqual(default_settings(task)['free_speed_mps'], 1.1)
        self.assertEqual(default_settings(task)['warning_density_p_m2'], 3)


class MultipleCaseTests(RequirementProgressionTests):
    async def test_output_confirmation_defines_indicators_before_model_selection(self):
        self.db.case_profiles.delete_many({})
        ctx = context()
        apply(ctx, {'changes': {'requirement.expected_outputs': []}})
        first = await self.service.generate([], '2026-12-31 18:00 至 2026-12-31 18:10', ctx, 'stub')
        card = first['confirmations'][0]
        self.assertEqual(card['subject_id'], 'progress-outputs')
        self.assertEqual(len(card['changes']['requirement.plan']['indicators']), 3)
        self.assertNotIn('experiment', card['changes']['requirement.plan'])
        apply(ctx, first)
        history = [message(first, 'output-stage')]
        choice = resolve_decision(history, {'message_id': 'output-stage', 'confirmation_id': card['id'], 'choice': 'accept'})
        second = await self.service.generate(history, '同意', ctx, 'stub', choice)
        self.assertEqual(len(second['changes']['requirement.plan']['indicators']), 3)
        self.assertNotIn('experiment', second['changes']['requirement.plan'])
        self.assertEqual(second['confirmations'][0]['subject_id'], 'progress-methods')

    async def test_old_text_steps_do_not_count_as_an_executable_experiment(self):
        self.db.case_profiles.delete_many({})
        ctx = context()
        legacy = research()
        legacy.pop('experiment')
        legacy['steps'] = [{'name': '结果对比与研判', 'method': 'hybrid', 'inputs': '待落实', 'output': '待落实'}]
        apply(ctx, {'changes': {'requirement.plan': legacy, 'task.timeWindow': '2026-12-31 18:00 至 2026-12-31 18:10'}})
        plan = await self.service.generate([], '继续', ctx, 'stub')
        self.assertEqual(plan['confirmations'][0]['subject_id'], 'progress-methods')
        self.assertEqual(len(plan['changes']['requirement.plan']['indicators']), 3)

    async def test_explicit_experiment_edit_runs_without_llm_or_progression(self):
        ctx = context()
        apply(ctx, {'changes': {'requirement.plan': research()}})
        plan = await self.service.generate([], 'CFS间距时间改为1.2秒，不要继续下一步', ctx, 'stub')
        self.assertEqual(plan['changes'], {})
        self.assertIn('实验控制', plan['reply'])
        self.assertEqual(plan['input_requests'], [])
        self.gateway.chat.assert_not_called()

    async def test_two_related_cases_are_proposed_without_adopting_either(self):
        self.db.case_profiles.insert_one({'_id': 'park-case', 'name': '上海公园节庆人群案例', 'summary': '游客在狭窄通道聚集排队，需要核对到达人数与通道有效宽度。', 'topic': '聚集风险', 'revision': 1})
        ctx = context()
        plan = await self.service.generate([], '2026-12-31 18:00 至 2027-01-01 02:00', ctx, 'stub')
        self.assertEqual({c['subject_id'] for c in plan['confirmations']}, {'bund-case', 'park-case'})
        self.assertNotIn('task.references', plan['changes'])
        apply(ctx, plan)
        history = [message(plan, 'two')]
        choice = resolve_decision(history, {'message_id': 'two', 'confirmation_id': plan['confirmations'][0]['id'], 'choice': 'accept'})
        next_plan = await self.service.generate(history, '同意', ctx, 'stub', choice)
        self.assertEqual(next_plan['confirmations'], [])
        self.assertIn('先确认上方', next_plan['reply'])

    async def test_previous_single_recommendation_can_be_supplemented(self):
        ctx = context()
        apply(ctx, {'changes': {'task.timeWindow': '2026-12-31 18:00 至 2026-12-31 18:10'}})
        first = await self.service.generate([], '继续', ctx, 'stub')
        first['retrieval']['items'].append({**first['retrieval']['items'][0], 'id': 'second-case', 'title': '已有的第二个相关候选'})
        apply(ctx, first)
        result = await self.service.generate([message(first, 'old')], '继续', ctx, 'stub')
        self.assertEqual([c['subject_id'] for c in result['confirmations']], ['second-case'])
