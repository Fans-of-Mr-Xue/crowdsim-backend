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
    def test_agent_type_ranges_follow_assigned_crowd_role(self):
        sampler = PopulationProfileSampler(
            seed=7,
            distributions={"crowd_role": [
                {"code": "calm", "label": "平稳人群", "percent": 50},
                {"code": "urgent", "label": "急迫人群", "percent": 50},
            ]},
        )
        sampler.configure_agent_types([
            {"id": "calm", "attributes": {"free_walking_speed": {"min": 0.8, "max": 0.8}}},
            {"id": "urgent", "attributes": {"free_walking_speed": {"min": 1.8, "max": 1.8}}},
        ])
        sampler.prepare_population(["person-1", "person-2"])
        profiles = [sampler.sample_profile("person-1"), sampler.sample_profile("person-2")]
        by_role = {profile.crowd_role: profile for profile in profiles}
        self.assertEqual(0.8, by_role["calm"].free_walking_speed)
        self.assertEqual(1.8, by_role["urgent"].free_walking_speed)


if __name__ == "__main__":
    unittest.main()
