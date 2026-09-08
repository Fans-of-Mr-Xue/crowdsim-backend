import unittest

from crowd_environment import CrowdEnvironment


class CrowdEnvironmentTests(unittest.TestCase):
    def test_density_levels_use_personal_threshold_and_area_density(self):
        classify = CrowdEnvironment.classify_density
        self.assertEqual("free", classify(2, 10, 0.2))
        self.assertEqual("busy", classify(6, 10, 0.2))
        self.assertEqual("crowded", classify(10, 10, 0.2))
        self.assertEqual("critical", classify(20, 10, 0.2))
        self.assertEqual("busy", classify(1, 10, 2.5))
        self.assertEqual("critical", classify(1, 10, 7.0))


if __name__ == "__main__":
    unittest.main()
