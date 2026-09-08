import unittest

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot, Observation
from crowdsim.core.state_updater import StateUpdater


def observation(person_id, crowding, speed=1.0, neighbours=()):
    motion = MotionSnapshot(person_id, 1.0, 0, 0, 0, 0, speed, "e", "e_0", 0, 0, 0, 2)
    return Observation(person_id, "s", 1.0, motion, tuple(neighbours), len(neighbours) + 1, 10.0, 0.1, crowding)


class StateUpdaterTests(unittest.TestCase):
    def test_updates_are_independent_of_iteration_order(self):
        updater = StateUpdater()
        profiles = {name: AgentProfile(name) for name in ("a", "b")}
        states = {"a": AgentState("a", stress=0.2), "b": AgentState("b", stress=0.8)}
        observations = {"a": observation("a", 0.8, neighbours=("b",)), "b": observation("b", 0.2, neighbours=("a",))}
        forward = updater.update_all(states, profiles, observations, 0.5)
        reverse = updater.update_all(dict(reversed(list(states.items()))), profiles, dict(reversed(list(observations.items()))), 0.5)
        self.assertEqual(forward["a"].stress, reverse["a"].stress)
        self.assertEqual(forward["b"].stress, reverse["b"].stress)

    def test_stress_recovers_and_fatigue_accumulates_then_recovers(self):
        updater = StateUpdater()
        profile = {"a": AgentProfile("a", recovery_seconds=20, endurance=0.5)}
        state = {"a": AgentState("a", stress=0.8, fatigue=0.2)}
        recovered = updater.update_all(state, profile, {"a": observation("a", 0, speed=0)}, 1.0)["a"]
        self.assertLess(recovered.stress, state["a"].stress)
        self.assertLess(recovered.fatigue, state["a"].fatigue)
        active = updater.update_all(state, profile, {"a": observation("a", 0, speed=1.0)}, 1.0)["a"]
        self.assertGreater(active.fatigue, state["a"].fatigue)


if __name__ == "__main__":
    unittest.main()
