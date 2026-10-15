"""Category-specific chat selections; no model, service or simulation IO."""
from copy import deepcopy
import tempfile
import unittest

from crowdsim.domain.observation_config import DEFERRED_METRIC_IDS
from crowdsim.domain.requirement_spec import (
    DECISION_OUTPUT_IDS, MACRO_METRIC_IDS, RequirementSpec, RequirementValidationError,
)
from crowdsim.infrastructure.observation_metrics import ObservationCollector
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from tests.test_requirement_location_v2 import located_requirement


def selection(problem_type, ids):
    raw = located_requirement(True)
    raw['research'] = {'problem_type': problem_type}
    raw['observation'].update(selection_schema_version=2, metric_ids=ids)
    return raw


class RequirementMetricSelectionTests(unittest.TestCase):
    def test_class_one_accepts_each_macro_summary_alone_or_all_six(self):
        for ids in [[name] for name in sorted(MACRO_METRIC_IDS)] + [
            ['concise-simulation-summary'], sorted(MACRO_METRIC_IDS) + ['concise-simulation-summary'],
        ]:
            with self.subTest(ids=ids):
                spec = RequirementSpec.parse(selection(1, ids))
                self.assertEqual(ids, spec.payload['observation']['metric_ids'])
                if 'concise-simulation-summary' in ids:
                    self.assertEqual('not_implemented', spec.capabilities()['metrics']['concise-simulation-summary'])

    def test_class_two_accepts_exactly_one_output_and_defers_its_generation(self):
        for name in DECISION_OUTPUT_IDS:
            spec = RequirementSpec.parse(selection(2, [name]))
            self.assertEqual('not_implemented', spec.capabilities()['metrics'][name])

    def test_wrong_category_duplicate_empty_or_malformed_choices_fail_validation(self):
        cases = [(1, ['evacuation-time']), (1, ['pedestrian-state-change']),
                 (1, ['concise-decision-advice']), (1, []), (1, ['global-speed'] * 2),
                 (1, [{}]), (1, ['unknown']), (2, ['global-speed']),
                 (2, ['concise-simulation-summary']), (2, sorted(DECISION_OUTPUT_IDS)),
                 (3, ['global-speed']), (4, ['detailed-decision-plan']), (True, ['global-speed'])]
        for problem_type, ids in cases:
            with self.subTest(problem_type=problem_type, ids=ids):
                with self.assertRaises(RequirementValidationError):
                    RequirementSpec.parse(selection(problem_type, ids))
        for version in (None, True, 1, 3, '2'):
            raw = selection(1, ['global-speed'])
            raw['observation']['selection_schema_version'] = version
            with self.assertRaises(RequirementValidationError):
                RequirementSpec.parse(raw)

    def test_legacy_record_round_trip_keeps_original_choices_and_fingerprint(self):
        raw = located_requirement(True)
        raw['research'] = {'problem_type': 1}
        raw['observation']['metric_ids'] = ['pedestrian-state-change', 'evacuation-time', *sorted(DECISION_OUTPUT_IDS)]
        spec = RequirementSpec.parse(raw)
        self.assertNotIn('selection_schema_version', spec.payload['observation'])
        with tempfile.TemporaryDirectory() as directory:
            repo = RequirementRepository(directory)
            record = repo.create(spec)
            loaded = repo.load(record['requirement_id'])
            self.assertEqual(record['fingerprint'], loaded['fingerprint'])
            self.assertEqual(raw, loaded['requirement'])
            edited = deepcopy(raw)
            edited['observation'].update(selection_schema_version=2, metric_ids=['concise-simulation-summary'])
            updated = repo.create(RequirementSpec.parse(edited))
            self.assertNotEqual(record['requirement_id'], updated['requirement_id'])
            self.assertEqual(raw, repo.load(record['requirement_id'])['requirement'])

    def test_output_only_and_mixed_requirements_remain_valid_without_inventing_artifacts(self):
        for ids in [[name] for name in DEFERRED_METRIC_IDS] + [['global-density', 'concise-simulation-summary']]:
            collector = ObservationCollector([(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)], ids)
            expected = ('global-density',) if 'global-density' in ids else ()
            self.assertEqual(expected, collector.metric_ids)
            self.assertEqual(set(expected), collector.computed_ids)
            result = collector.result(status='completed', reason='time_limit', recorded_samples=0)
            for name in set(ids) & set(DEFERRED_METRIC_IDS):
                self.assertEqual('not_implemented', result['metrics'][name]['status'])


if __name__ == '__main__':
    unittest.main()
