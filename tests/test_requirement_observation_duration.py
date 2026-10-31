"""Duration contract checks without starting SUMO or network services."""
from copy import deepcopy
import unittest

from crowdsim.domain.requirement_spec import (
    RequirementSpec, RequirementValidationError, requirement_observation_duration,
)
from tests.test_requirement_location_v2 import located_requirement


class RequirementObservationDurationTests(unittest.TestCase):
    def requirement(self, duration=600):
        raw = located_requirement(True)
        raw.update(research={"problem_type": 1}, experiment={"duration_seconds": duration, "repetitions": 5})
        return raw

    def test_positive_limits_are_accepted_without_changing_the_saved_payload(self):
        for duration in (0.1, 1.25, 600, 1800, 7200):
            with self.subTest(duration=duration):
                raw = self.requirement(duration)
                original = deepcopy(raw)
                parsed = RequirementSpec.parse(raw)
                self.assertEqual(float(duration), requirement_observation_duration(parsed.payload))
                self.assertEqual(original, raw)
                self.assertEqual(original, parsed.payload)

    def test_invalid_limits_are_rejected_before_a_requirement_is_saved(self):
        for duration in (0, -1, True, False, None, "600", float("nan"), float("inf"), {}, 10 ** 400):
            with self.subTest(duration=str(duration)), self.assertRaisesRegex(
                RequirementValidationError, "experiment.duration_seconds"
            ):
                RequirementSpec.parse(self.requirement(duration))
        raw = self.requirement()
        raw["experiment"] = []
        with self.assertRaisesRegex(RequirementValidationError, "experiment must be an object"):
            RequirementSpec.parse(raw)

    def test_old_records_and_other_problem_classes_keep_their_previous_timing(self):
        raw = self.requirement()
        del raw["experiment"]["duration_seconds"]
        self.assertIsNone(requirement_observation_duration(RequirementSpec.parse(raw).payload))
        for problem_type in (2, 3, 4):
            raw = self.requirement()
            raw["research"]["problem_type"] = problem_type
            self.assertIsNone(requirement_observation_duration(RequirementSpec.parse(raw).payload))
        raw = self.requirement()
        raw["schema_version"] = 1
        self.assertIsNone(requirement_observation_duration(RequirementSpec.parse(raw).payload))
