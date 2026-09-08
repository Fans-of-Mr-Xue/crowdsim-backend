import unittest

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, GroupRecord, MotionSnapshot, Observation
from crowdsim.domain.group_manager import GroupManager


class GroupManagerTests(unittest.TestCase):
    def test_group_uses_three_real_entities_and_no_hidden_location(self):
        manager = GroupManager()
        states = {name: AgentState(name) for name in ("a", "b", "c")}
        manager.register(GroupRecord("g", ["a", "b", "c"], "a", "meet"), states)
        manager.update_groups(states)
        self.assertEqual(3, len(manager.groups["g"].member_ids))
        self.assertEqual(["b", "c"], states["a"].companion_ids)
        motion = MotionSnapshot("a", 0, 0, 0, 0, 0, 0, "e", "e_0", 0, 0, 0, 2)
        hidden = Observation("a", "s", 0, motion, (), 1, 10, 0.1, 0.1)
        visible = Observation("a", "s", 0, motion, ("b",), 2, 10, 0.2, 0.2)
        profile = AgentProfile("a", following_tendency=1, group_cohesion=1)
        self.assertIsNone(manager.coordination_candidate("a", profile, hidden))
        self.assertEqual("follow_group", manager.coordination_candidate("a", profile, visible))

    def test_group_may_be_registered_before_members_depart(self):
        manager = GroupManager()
        manager.register(GroupRecord("g", ["a", "b"]), {"a", "b"})
        states = {"a": AgentState("a")}
        manager.update_groups(states)
        self.assertEqual(["b"], states["a"].companion_ids)


if __name__ == "__main__":
    unittest.main()
