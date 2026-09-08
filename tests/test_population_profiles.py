import unittest

from crowdsim.domain.population_profiles import PopulationProfileSampler


class PopulationProfileTests(unittest.TestCase):
    def test_same_id_and_seed_are_reproducible(self):
        first = PopulationProfileSampler(seed=7).sample_profile("person-1")
        second = PopulationProfileSampler(seed=7).sample_profile("person-1")
        self.assertEqual(first, second)

    def test_background_language_and_nationality_do_not_change_active_values(self):
        profile = PopulationProfileSampler(seed=7).sample_profile("person-1")
        active = (profile.risk_tolerance, profile.familiarity, profile.authority_compliance, profile.following_tendency)
        changed_background = profile.__class__(**{**vars(profile), "nationality": "changed", "native_language": "changed"})
        self.assertEqual(active, (changed_background.risk_tolerance, changed_background.familiarity, changed_background.authority_compliance, changed_background.following_tendency))


if __name__ == "__main__":
    unittest.main()
