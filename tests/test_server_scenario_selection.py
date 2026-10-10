import argparse
from pathlib import Path
import tempfile
import unittest

from crowdsim_overlay_server import SCENARIO_DIR, resolve_service_scenario


def arguments(*, scenario="research", config=None, ped_routes=None):
    return argparse.Namespace(scenario=scenario, config=config, ped_routes=ped_routes)


class ServerScenarioSelectionTests(unittest.TestCase):
    def test_research_preset_pairs_research_config_and_demand(self):
        selection = resolve_service_scenario(arguments())

        self.assertEqual("research", selection.name)
        self.assertEqual((SCENARIO_DIR / "bund.research.sumocfg").resolve(), selection.config_path)
        self.assertEqual(((SCENARIO_DIR / "bund_ped.rou.xml").resolve(),), selection.pedestrian_route_files)
        self.assertEqual("configurable", selection.demand_mode)
        self.assertIsNone(selection.timeline_end_seconds)

    def test_hotspot_preset_pairs_hotspot_config_and_demand(self):
        selection = resolve_service_scenario(arguments(scenario="hotspot"))

        self.assertEqual("hotspot", selection.name)
        self.assertEqual((SCENARIO_DIR / "bund.hotspot.sumocfg").resolve(), selection.config_path)
        self.assertEqual(((SCENARIO_DIR / "bund_hotspot.rou.xml").resolve(),), selection.pedestrian_route_files)
        self.assertEqual("generated_hotspot", selection.demand_mode)
        self.assertEqual(1800.0, selection.timeline_end_seconds)

    def test_known_config_without_routes_uses_its_matching_preset(self):
        config = SCENARIO_DIR / "bund.hotspot.sumocfg"

        selection = resolve_service_scenario(arguments(config=config))

        self.assertEqual("hotspot", selection.name)
        self.assertEqual(((SCENARIO_DIR / "bund_hotspot.rou.xml").resolve(),), selection.pedestrian_route_files)
        self.assertEqual("generated_hotspot", selection.demand_mode)

    def test_east_nanjing_preset_uses_its_own_network_and_generated_demand(self):
        selection = resolve_service_scenario(arguments(scenario="east-nanjing-road"))
        self.assertEqual("east-nanjing-road", selection.location_id)
        self.assertEqual("east_nanjing.sumocfg", selection.config_path.name)
        self.assertEqual("generated_hotspot", selection.demand_mode)
        self.assertEqual("chen_yi_square", selection.hotspot_demand_spec.hotspot_id)
        self.assertEqual(selection.config_path.parent / "crowd_hotspots.json", selection.hotspot_demand_spec.config_path)
        self.assertEqual("/static/crowd_sim/east_nanjing_road_network.json", selection.road_network_url)
        self.assertEqual(1800.0, selection.timeline_end_seconds)

    def test_known_east_config_keeps_its_independent_hotspot_specification(self):
        preset = resolve_service_scenario(arguments(scenario="east-nanjing-road"))
        selection = resolve_service_scenario(arguments(config=preset.config_path))
        self.assertEqual(preset.hotspot_demand_spec, selection.hotspot_demand_spec)
        self.assertEqual("generated_hotspot", selection.demand_mode)

    def test_custom_config_requires_explicit_pedestrian_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "custom.sumocfg"
            config.write_text("<configuration><input /></configuration>", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "requires at least one --ped-routes"):
                resolve_service_scenario(arguments(config=config))

    def test_route_must_be_referenced_by_custom_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "custom.sumocfg"
            configured_route = root / "configured.rou.xml"
            wrong_route = root / "wrong.rou.xml"
            configured_route.write_text("<routes />", encoding="utf-8")
            wrong_route.write_text("<routes />", encoding="utf-8")
            config.write_text(
                '<configuration><input><route-files value="configured.rou.xml" /></input></configuration>',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not referenced"):
                resolve_service_scenario(arguments(config=config, ped_routes=[wrong_route]))


if __name__ == "__main__":
    unittest.main()
