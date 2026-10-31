"""Critical checks for source fidelity, real SUMO motion and paired analysis."""
from copy import deepcopy
import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.generate_post_memorial_fixture import normalize
from postanalysis_api.schemas.datasets import validate_dataset
from postanalysis_api.workers.analysis import paired_effect
from postanalysis_api.workers.causal import discover
from postanalysis_api.workers.memorial_scenario import compile_routes, source_cohorts

ROOT = Path(__file__).resolve().parents[1]


class PostPipelineTests(unittest.TestCase):
    def test_measurement_uses_declared_sampling_period_without_overcounting_person_time(self):
        from postanalysis_api.workers.measurement import measure
        persons = ''.join(f'<person id="p{index}" x="1" y="1" speed="0" angle="0" edge="178411801#0"/>' for index in range(4))
        fcd = '<fcd-export>' + ''.join(f'<timestep time="{second}">{persons}</timestep>' for second in (0, 0.5, 5, 5.5)) + '</fcd-export>'
        chunks = []
        repository = SimpleNamespace(append_trajectory=lambda archive, sequence, frames: chunks.extend(frames))
        areas = {key: SimpleNamespace(area=1) for key in ('A', 'B', 'C', 'global')}
        with tempfile.TemporaryDirectory() as folder, patch('postanalysis_api.workers.measurement.geometry', return_value=areas), patch('postanalysis_api.workers.measurement.cell_area', return_value=1), patch('postanalysis_api.workers.measurement.region_cells', return_value={'A': {(0, 0)}, 'B': set(), 'C': set()}):
            path = Path(folder) / 'fcd.xml'
            path.write_text(fcd, encoding='utf-8')
            result = measure(path, 10, {}, repository, [])
        self.assertEqual(40, result['metrics']['risk_exposure'])
        self.assertEqual(10, result['metrics']['congestion_duration'])
        self.assertEqual([0, 5], [frame['timeSeconds'] for frame in chunks])

    def test_canonical_source_round_trips_without_fabricating_missing_metrics(self):
        path = ROOT / "docs/examples/shanghai_memorial_post_dataset.json"
        artifact = json.loads(path.read_text(encoding="utf-8"))
        source = ROOT.parent / "CrowdSim/frontend/src/views/crowdSim/postanalysis/Simulation_data_ShanghaiMemorialTower_detail.md"
        self.assertEqual(normalize(source), artifact)
        result = validate_dataset(artifact)
        self.assertEqual(60, len(result["observations"]))
        self.assertEqual(19800, result["metadata"]["durationSeconds"])
        self.assertEqual(300, max(sum(row["population"] for row in result["observations"] if row["timeSeconds"] == time)
                                  for time in {row["timeSeconds"] for row in result["observations"]}))
        self.assertTrue(all(type(row[key]) is int for row in result["metadata"]["sourceRecords"] for key in ("population", "intervalEntered", "intervalExited", "populationResidual")))
        self.assertTrue(all(row["density"] is None and row["meanSpeed"] is None and row["pressureProxy"] is None for row in result["observations"]))
        self.assertTrue(source_cohorts(result))

    def test_ablated_guidance_changes_only_declared_routing_module(self):
        dataset = json.loads((ROOT / "docs/examples/shanghai_memorial_post_dataset.json").read_text(encoding="utf-8"))
        actions = [{"id": "open", "targetRegionId": "C", "actionType": "open_gate", "atSeconds": 13800, "parameters": {}},
                   {"id": "guide", "targetRegionId": "C", "actionType": "guidance", "atSeconds": 13800,
                    "parameters": {"compliance": 1, "delaySeconds": 0}}]
        with tempfile.TemporaryDirectory() as folder:
            full = compile_routes(dataset, 10, actions, 19800, Path(folder) / "full.xml")
            ablated_actions = deepcopy(actions)
            ablated_actions[1]["parameters"]["compliance"] = 0
            ablated = compile_routes(dataset, 10, ablated_actions, 19800, Path(folder) / "ablated.xml")
        self.assertEqual(full["cohortCount"], ablated["cohortCount"])
        self.assertEqual(full["controls"]["gateOpenSeconds"], ablated["controls"]["gateOpenSeconds"])
        self.assertGreater(sum(row["exitRegionId"] == "C" for row in full["cohorts"]),
                           sum(row["exitRegionId"] == "C" for row in ablated["cohorts"]))
        self.assertEqual([row["id"] for row in full["cohorts"]], [row["id"] for row in ablated["cohorts"]])
        self.assertEqual([(row["holdingEdge"], row["holdingPosition"]) for row in full["cohorts"]],
                         [(row["holdingEdge"], row["holdingPosition"]) for row in ablated["cohorts"]])
        self.assertGreater(len({round(row["holdingPosition"], 1) for row in full["cohorts"]}), 100)

    def test_paired_effect_uses_seed_intersection_and_does_not_fill_missing(self):
        runs = [{"seed": seed, "planId": group, "status": "completed", "metrics": {"peak_density": value}}
                for seed, group, value in ((1, "F-00", 5), (2, "F-00", 8), (1, "CF-01", 3), (3, "CF-01", 1))]
        effect = paired_effect(runs, "F-00", "CF-01", "peak_density")
        self.assertEqual([1], effect["seeds"])
        self.assertEqual(-2, effect["estimate"])
        self.assertIsNone(effect["ci95"])

    def test_pc_uses_independent_seeds_and_detects_gaussian_chain_skeleton(self):
        rng, runs = random.Random(7), []
        features = [{"id": key, "metricId": "peak_density", "regionId": "global", "windowStartSeconds": 0, "windowEndSeconds": 20}
                    for key in ("x", "y", "z")]
        for seed in range(1000):
            x = rng.gauss(0, 1)
            y = x + rng.gauss(0, 1)
            z = y + rng.gauss(0, 1)
            runs.append({"planId": "F-00", "status": "completed", "seed": seed, "featureValues": {"x": x, "y": y, "z": z}})
        graph = discover(features, runs)
        self.assertEqual("computed", graph["status"])
        self.assertEqual({frozenset(("x", "y")), frozenset(("y", "z"))},
                         {frozenset((edge["source"], edge["target"])) for edge in graph["edges"]})
        self.assertEqual("insufficient_data", discover(features, runs[:5])["status"])


if __name__ == "__main__":
    unittest.main()
