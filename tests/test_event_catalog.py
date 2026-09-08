import unittest

from event_catalog import event_defaults
from event_manager import EventManager


class FakeNetwork:
    def lonlat_to_xy(self, lon, lat):
        return lon, lat


class EventCatalogTests(unittest.TestCase):
    def test_known_types_are_only_parameter_presets(self):
        self.assertGreater(event_defaults("fire")["stress"], event_defaults("generic")["stress"])
        self.assertGreater(event_defaults("obstacle")["speed"], event_defaults("generic")["speed"])

    def test_unknown_type_uses_generic_defaults(self):
        self.assertEqual(event_defaults("generic"), event_defaults("anything"))

    def test_event_grows_and_decays(self):
        manager = EventManager(FakeNetwork())
        manager.apply({"lng": 1, "lat": 1, "intensity": 1, "duration": 10,
                       "growthSeconds": 2, "decaySeconds": 2}, 0, 0)
        manager.step(1)
        self.assertEqual("growing", manager.events[0].phase)
        self.assertAlmostEqual(0.5, manager.events[0].intensity)
        manager.step(9)
        self.assertEqual("decaying", manager.events[0].phase)
        manager.step(10)
        self.assertFalse(manager.events)


if __name__ == "__main__":
    unittest.main()
