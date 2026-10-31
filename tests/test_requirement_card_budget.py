import unittest
from data_service.services.requirement_card_budget import limit_requirement_cards


class RequirementCardBudgetTests(unittest.TestCase):
    def test_shared_limit_prioritizes_environment_and_time_and_removes_duplicate_scope(self):
        environment = {'kind': 'environment', 'subject_id': 'a'}
        proposals = [{'kind': 'proposal', 'subject_id': str(i)} for i in range(4)]
        time = {'target': 'scope', 'title': '补充时间', 'widget': 'time-window'}
        duplicate = {'target': 'scope', 'title': '确认研究范围'}
        plan = limit_requirement_cards({'changes': {'task.location': '纪念塔'}, 'confirmations': proposals + [environment], 'input_requests': [duplicate, time, {'target': 'goal', 'title': '重点问题'}]})
        self.assertEqual(len(plan['confirmations']) + len(plan['input_requests']), 3)
        self.assertIn(environment, plan['confirmations'])
        self.assertIn(time, plan['input_requests'])
        self.assertNotIn(duplicate, plan['input_requests'])
        self.assertEqual(plan['changes'], {'task.location': '纪念塔'})

    def test_each_card_category_uses_the_same_limit(self):
        for count in range(7):
            plan = limit_requirement_cards({'confirmations': [{'kind': 'case', 'subject_id': str(i)} for i in range(count)],
                                           'input_requests': [{'target': 'inputs', 'title': str(i)} for i in range(6 - count)]})
            self.assertEqual(len(plan['confirmations']) + len(plan['input_requests']), 3)
