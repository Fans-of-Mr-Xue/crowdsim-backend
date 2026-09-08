import unittest

from crowdsim.domain.crowdsim_models import MotionSnapshot
from crowdsim.environment.hazard_model import HazardModel, HazardZone
from crowdsim.environment.information_model import InformationModel
from crowdsim.environment.intervention_executor import InterventionExecutor


class HazardModelTests(unittest.TestCase):
    def test_hazard_speed_limit_is_explicit_and_restorable(self):
        model = HazardModel()
        motion = MotionSnapshot("p", 0, 0, 0, 0, 0, 1, "e", "e_0", 0, 0, 0, 2)
        model.add(HazardZone("h", "flood", 0, 0, 10, 1, 0.75))
        self.assertAlmostEqual(0.25, model.speed_limit_for(motion, 1.0))
        model.replace_static_points(())
        self.assertIsNone(model.speed_limit_for(motion, 1.0))

    def test_unsupported_physical_intervention_is_rejected(self):
        executor = InterventionExecutor(InformationModel())
        with self.assertRaises(ValueError):
            executor.apply_command("dynamic_polygon_closure", {}, 0)


if __name__ == "__main__":
    unittest.main()
