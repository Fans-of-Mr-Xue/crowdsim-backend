import unittest

from crowdsim.environment.event_catalog import event_defaults
from crowdsim.environment.event_manager import EventManager
from crowdsim.environment.hazard_model import HazardModel, HazardZone
from crowdsim.environment.information_model import InformationModel
from crowdsim.environment.intervention_executor import InterventionExecutor


class FakeNetwork:
    def lonlat_to_xy(self, lon, lat):
        return lon, lat


class EventCatalogTests(unittest.TestCase):
    def test_information_and_physical_presets_are_separate(self):
        self.assertFalse(event_defaults("rumor")["physical"])
        self.assertFalse(event_defaults("alarm")["physical"])
        self.assertTrue(event_defaults("fire")["physical"])
        self.assertNotIn("density", event_defaults("generic"))

    def test_unknown_type_uses_generic_defaults(self):
        self.assertEqual(event_defaults("generic"), event_defaults("anything"))

    def test_event_grows_decays_and_serializes_density_as_deprecated_one(self):
        manager = EventManager(FakeNetwork())
        manager.apply({"lng": 1, "lat": 1, "intensity": 1, "duration": 10, "growthSeconds": 2, "decaySeconds": 2}, 0, 0)
        manager.step(1)
        self.assertEqual("growing", manager.events[0].phase)
        self.assertAlmostEqual(0.5, manager.events[0].intensity)
        self.assertEqual(1.0, manager.serialize(1)[0]["density_multiplier"])
        manager.step(10)
        self.assertFalse(manager.events)

    def test_guidance_and_observe_only_do_not_change_hazard(self):
        hazards = HazardModel()
        hazards.add(HazardZone("h", "flood", 0, 0, 10, 0.5, 0.7))
        before = vars(hazards.zones["h"]).copy()
        executor = InterventionExecutor(InformationModel())
        self.assertFalse(executor.apply_command("observe_only", {}, 0)["physical_change"])
        self.assertFalse(executor.apply_command("police_guidance", {}, 0)["physical_change"])
        self.assertEqual(before, vars(hazards.zones["h"]))


if __name__ == "__main__":
    unittest.main()
