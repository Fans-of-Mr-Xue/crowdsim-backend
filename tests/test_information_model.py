import unittest

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot
from crowdsim.environment.information_model import InformationMessage, InformationModel


def motion(person_id, x):
    return MotionSnapshot(person_id, 0, x, 0, x, 0, 0, "e", "e_0", x, 0, 0, 2)


class InformationModelTests(unittest.TestCase):
    def test_delay_range_expiry_dedup_and_trust(self):
        model = InformationModel()
        model.publish(InformationMessage("m1", "alarm", "official", 0, 2, 7, x=0, y=0, radius=5, event_id="alarm"))
        motions = {"inside": motion("inside", 1), "outside": motion("outside", 10)}
        profiles = {"inside": AgentProfile("inside", authority_compliance=1, information_trust={"official": 1}), "outside": AgentProfile("outside", information_trust={"official": 1})}
        states = {name: AgentState(name) for name in motions}
        self.assertFalse(model.deliver(1, motions, profiles, states))
        records = model.deliver(2, motions, profiles, states)
        self.assertEqual(["inside"], [record.person_id for record in records])
        self.assertTrue(records[0].trusted)
        self.assertFalse(model.deliver(3, motions, profiles, states))
        model.expire(7, states)
        self.assertNotIn("alarm", states["inside"].known_events)

    def test_native_language_does_not_change_understanding(self):
        model = InformationModel()
        model.publish(InformationMessage("m", "guide", "companions", 0, 0, 5, recipient_ids=("a", "b")))
        motions = {name: motion(name, 0) for name in ("a", "b")}
        profiles = {"a": AgentProfile("a", native_language="zh", information_trust={"companions": 1}), "b": AgentProfile("b", native_language="other", information_trust={"companions": 1})}
        states = {name: AgentState(name) for name in motions}
        records = model.deliver(0, motions, profiles, states)
        self.assertTrue(all(record.understood for record in records))


if __name__ == "__main__":
    unittest.main()
