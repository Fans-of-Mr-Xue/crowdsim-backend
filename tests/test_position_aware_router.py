from pathlib import Path
import unittest

from crowdsim.decision.position_aware_router import (
    EdgePosition,
    PositionAwarePedestrianRouter,
    PositionRouteUnavailable,
)
from crowdsim.infrastructure.network_adapter import ResearchNetwork


ROOT = Path(__file__).resolve().parents[1]


class PositionAwarePedestrianRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = ResearchNetwork(
            str(ROOT / "scenarios" / "shanghai_bund" / "bund.net.xml")
        )
        cls.router = PositionAwarePedestrianRouter(cls.network)

    def test_exact_ring_position_distinguishes_monument_entrances(self):
        target = EdgePosition("679361567#1", 90.0)
        from_west = self.router.route_from_endpoint(
            ("178411801#0", 1),
            target,
            forbidden_edges=("177931018#0",),
        )
        from_south = self.router.route_from_endpoint(
            ("177931018#0", 1),
            target,
            forbidden_edges=("178411801#0",),
        )

        self.assertLess(from_south.distance_m, from_west.distance_m)
        self.assertAlmostEqual(31.78, from_south.distance_m, places=2)
        self.assertGreater(from_west.distance_m - from_south.distance_m, 20.0)

    def test_position_query_reuses_endpoint_trees(self):
        before = self.router.diagnostics()["tree_builds"]
        self.router.route(
            EdgePosition("906417852#7", 2.0),
            EdgePosition("679361567#1", 70.0),
        )
        after_first = self.router.diagnostics()["tree_builds"]
        self.router.route(
            EdgePosition("906417852#7", 8.0),
            EdgePosition("679361567#1", 75.0),
        )
        after_second = self.router.diagnostics()["tree_builds"]

        self.assertGreaterEqual(after_first, before)
        self.assertEqual(after_first, after_second)
        self.assertGreater(self.router.diagnostics()["tree_cache_hits"], 0)

    def test_forced_portal_route_contains_exactly_the_selected_portal(self):
        route = self.router.route_via_edges(
            EdgePosition("906417852#7", 5.0),
            EdgePosition("679361567#1", 80.0),
            ("177931018#0",),
            forbidden_edges=("178411801#0",),
        )

        self.assertEqual("906417852#7", route.edges[0])
        self.assertEqual("679361567#1", route.edges[-1])
        self.assertEqual(1, route.edges.count("177931018#0"))
        self.assertNotIn("178411801#0", route.edges)

    def test_explicit_portal_orientation_is_honoured(self):
        start = EdgePosition("906417852#7", 5.0)
        target = EdgePosition("679361567#1", 80.0)
        self.router.route_via_edges(
            start,
            target,
            ("177931018#0",),
            via_orientations=(0,),
            forbidden_edges=("178411801#0",),
        )
        with self.assertRaises(PositionRouteUnavailable):
            self.router.route_via_edges(
                start,
                target,
                ("177931018#0",),
                via_orientations=(1,),
                forbidden_edges=("178411801#0",),
            )


if __name__ == "__main__":
    unittest.main()
