"""Validate initial crowd placement and initialization with no SUMO process."""

from collections import Counter, defaultdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock
import xml.etree.ElementTree as ET

import sumolib

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.domain.person_parameters import HOTSPOT_ENTRY_EDGE_PARAM, HOTSPOT_TARGET_EDGE_PARAM
from crowdsim.infrastructure.sumo_adapter import SumoStepResult
from crowdsim.infrastructure.websocket_server import OverlayServer
from crowdsim.scenarios.generated_hotspot_demand import HotspotDemandSpec


ROOT = Path(__file__).resolve().parents[1]
BUND = ROOT / "scenarios/shanghai_bund"


class InitialHotspotDemandTests(unittest.TestCase):
    def test_initial_crowd_is_uniform_and_each_person_reaches_the_assigned_target(self):
        hotspot = json.loads((ROOT / "config/crowd_hotspots.json").read_text())["hotspots"][1]
        network = sumolib.net.readNet(str(BUND / "bund.net.xml"), withInternal=True, withPedestrianConnections=True)
        spec = HotspotDemandSpec(BUND / "bund_ped.rou.xml", ROOT / "config/crowd_hotspots.json")
        with tempfile.TemporaryDirectory() as directory:
            output, report = spec.generate(directory, BUND / "bund.net.xml", 800)
            people = ET.parse(output).getroot().findall("person")
        self.assertEqual(800, len(people))
        self.assertTrue(report["initial_population"])
        self.assertEqual([0.0, 0.0], report["planned_departure_window_seconds"])
        self.assertEqual([600, 800], report["activity_window_seconds"])
        self.assertEqual([800, 890], report["visitor_release_window_seconds"])
        positions = defaultdict(list)
        ring = set(hotspot["target_edges"])
        portals = set(hotspot["entry_edges"])
        scene = ring | portals | set(hotspot["park_access_edges"])
        self.assertEqual(scene, set(hotspot["visitor_spawn_edges"]))
        self.assertEqual(43, len(scene))
        self.assertIn("huangpu_park_j05_j15", scene)
        ring_spawns = 0
        for person in people:
            self.assertEqual("0.00", person.get("depart"))
            params = {p.get("key"): p.get("value") for p in person.findall("param")}
            walks = person.findall("walk")
            self.assertEqual(2, len(walks))
            inbound, outbound = (w.get("edges").split() for w in walks)
            source = inbound[0]
            positions[source].append(float(person.get("departPos")))
            self.assertIn(source, scene)
            self.assertEqual(params[HOTSPOT_TARGET_EDGE_PARAM], inbound[-1])
            self.assertEqual(inbound[-1], outbound[0])
            self.assertIn(outbound[-1], hotspot["visitor_destination_edges"])
            self.assertTrue(set(inbound + outbound).issubset(scene))
            self.assertFalse(set(inbound + outbound) & set(hotspot["excluded_edges"]))
            if source in ring:
                ring_spawns += 1
                self.assertTrue(set(inbound).issubset(ring))
                self.assertNotIn(HOTSPOT_ENTRY_EDGE_PARAM, params)
            elif source in portals:
                self.assertEqual(source, params[HOTSPOT_ENTRY_EDGE_PARAM])
                self.assertTrue(set(inbound[1:]).issubset(ring))
            else:
                self.assertEqual(1, len(set(inbound) & portals))
        self.assertGreater(ring_spawns, 0)
        self.assertEqual(800 - ring_spawns, sum(report["entry_counts"].values()))
        self.assertGreater(len(positions["monument_m3"]), 0)
        # Full edge lengths determine density, rather than one equal quota per
        # road. Largest-remainder quotas differ from the ideal by less than one.
        total_length = sum(network.getEdge(e).getLength() for e in scene)
        for edge in scene:
            length = network.getEdge(edge).getLength()
            count = len(positions[edge])
            self.assertLess(abs(count - 800 * length / total_length), 1.0)
            margin = min(2.0, length * 0.25)
            usable = length - 2 * margin
            for index, position in enumerate(sorted(positions[edge])):
                # XML positions are rounded to centimeters.
                self.assertGreaterEqual(position + 0.0051, margin + index * usable / count)
                self.assertLessEqual(position - 0.0051, margin + (index + 1) * usable / count)

    def test_short_connector_can_spawn_at_large_population_without_negative_positions(self):
        spec = HotspotDemandSpec(BUND / "bund_ped.rou.xml", ROOT / "config/crowd_hotspots.json")
        with tempfile.TemporaryDirectory() as directory:
            output, report = spec.generate(directory, BUND / "bund.net.xml", 10000)
            people = ET.parse(output).getroot().findall("person")
        self.assertEqual(10000, len(people))
        counts = Counter(p.find("walk").get("edges").split()[0] for p in people)
        self.assertEqual(report["spawn_edge_counts"], dict(counts))
        self.assertEqual(43, len(counts))
        short_edge_people = [p for p in people if p.find("walk").get("edges").split()[0] == "906417851#1"]
        self.assertGreater(len(short_edge_people), 0)
        for person in short_edge_people:
            self.assertGreaterEqual(float(person.get("departPos")), 0.05)
            self.assertLessEqual(float(person.get("departPos")), 0.15)


class InitialPopulationBoundaryTests(unittest.TestCase):
    def runtime(self, planned=("a", "b"), initial=True):
        runtime = object.__new__(SimulationRuntime)
        runtime.demand_generation_report = {"initial_population": initial}
        runtime.population = SimpleNamespace(ledger=SimpleNamespace(planned_ids=planned))
        runtime.adapter = Mock()
        return runtime

    def test_ready_snapshot_contains_every_person_at_real_engine_time(self):
        runtime = self.runtime()
        empty = SumoStepResult(0, {}, {}, (), ())
        populated = SumoStepResult(0.5, {"a": Mock(), "b": Mock()}, {}, ("a", "b"), ())
        runtime.adapter.step.return_value = populated
        result = runtime._materialize_initial_population(empty)
        self.assertIs(populated, result)
        self.assertEqual(0.5, result.time_seconds)
        runtime.adapter.step.assert_called_once_with()

    def test_empty_already_loaded_and_scheduled_demands_do_not_advance(self):
        for planned, initial in (((), True), (("a",), True), (("a", "b"), False)):
            with self.subTest(planned=planned, initial=initial):
                runtime = self.runtime(planned, initial)
                snapshot = SumoStepResult(0, {"a": Mock()} if planned == ("a",) else {}, {}, (), ())
                self.assertIs(snapshot, runtime._materialize_initial_population(snapshot))
                runtime.adapter.step.assert_not_called()

    def test_incomplete_initial_population_fails_instead_of_silently_spawning_later(self):
        runtime = self.runtime()
        runtime.adapter.step.return_value = SumoStepResult(0.5, {"a": Mock()}, {}, ("a",), ())
        with self.assertRaisesRegex(RuntimeError, "1 persons missing"):
            runtime._materialize_initial_population(SumoStepResult(0, {}, {}, (), ()))


class InitialPopulationFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_is_followed_by_frozen_population_update_before_start(self):
        runtime = SimpleNamespace(
            performance=MagicMock(),
            demand_generation_report={"initial_population": True},
            init_frame=Mock(return_value={"type": "init", "runtime_state": "READY"}),
            frame=Mock(return_value={"type": "update", "runtime_state": "READY", "step_seconds": 0.5,
                                     "pedestrians": [{"id": "a"}, {"id": "b"}]}),
        )
        messages = []
        server = OverlayServer(runtime, "127.0.0.1", 0, requirement_repository=Mock())
        server.client = SimpleNamespace(send=None)

        async def capture(message):
            messages.append(json.loads(message))

        server.client.send = capture
        await server._send_init("initialize")
        self.assertEqual(["init", "update"], [m["type"] for m in messages])
        self.assertEqual("initialize", messages[0]["request_id"])
        self.assertEqual("READY", messages[1]["runtime_state"])
        self.assertEqual(2, len(messages[1]["pedestrians"]))
        self.assertEqual(0.5, messages[1]["step_seconds"])
