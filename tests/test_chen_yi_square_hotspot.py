"""Offline checks for the Chen Yi Square preset; never launch SUMO or open sockets."""

from collections import Counter
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from sumolib.geomhelper import positionAtShapeOffset
from traci import constants as tc

from crowdsim.core.population_manager import PopulationManager
from crowdsim.core.simulation_runtime import RuntimeState
from crowdsim.decision.hotspot_route_choice import HotspotRouteChoice
from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.decision.route_provider import RouteProvider
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot
from crowdsim.domain.person_parameters import (
    HOTSPOT_ID_PARAM, HOTSPOT_TARGET_EDGE_PARAM, HOTSPOT_RELEASE_TIME_PARAM,
)
from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.environment.hotspot_catalog import HotspotCatalog
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.sumo_adapter import SumoStepResult
from crowdsim.infrastructure.websocket_server import OverlayServer
from crowdsim_overlay_server import build_requirement_runtime, build_runtime, parse_args
from tests.test_requirement_scenario_runtime import east_requirement
from tests.test_requirement_submission import CapturingClient


ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "scenarios/east_nanjing_road"
CORE = {"R25b", "R29", "R26b", "R30"}
ENTRIES = {"R13", "R18", "R25a", "R26a", "R25c", "R26c"}
DESTINATIONS = {"R01", "R02", "R03", "R22", "R31"}


class DemandSnapshotAdapter:
    """Supply initial snapshots from XML to exercise application setup only."""

    def __init__(self, config_path, *, sumo_binary=None, extra_args=()):
        self.config_path = config_path
        self.extra_args = list(extra_args)
        self.diagnostics = {"engine": "offline-unit-test"}
        self.speeds = {}
        self.initial = None

    def start(self):
        routes = self.extra_args[self.extra_args.index("--route-files") + 1].split(",")
        network = ResearchNetwork(str(SCENE / "east_nanjing.net.xml"))
        persons = {}
        for path in routes:
            for row in ET.parse(path).getroot().findall("person"):
                edge_id = row.find("walk").get("edges").split()[0]
                position = float(row.get("departPos"))
                lane = network.edges[edge_id].getLanes()[0]
                x, y = positionAtShapeOffset(lane.getShape(), position)
                person_id = row.get("id")
                persons[person_id] = MotionSnapshot(
                    person_id, .5, x, y, *network.xy_to_lonlat(x, y),
                    1.0, edge_id, lane.getID(), position, 0, 0, tc.STAGE_WALKING,
                    remaining_stage_count=len(row.findall("walk")),
                )
        self.initial = SumoStepResult(.5, persons, {}, tuple(persons), ())
        return SumoStepResult(0, {}, {}, (), ())

    def step(self):
        return self.initial

    def set_person_speed(self, person_id, speed):
        self.speeds[person_id] = speed

    def display_edge(self, person_id, edge_id):
        return edge_id

    def close(self):
        pass


class ChenYiSquareDemandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("crowdsim.infrastructure.sumo_adapter.discover_sumo_binary", return_value="unused"):
            cls.spec = build_runtime(parse_args(["--scenario", "east-nanjing-road"])).hotspot_demand_spec
        cls.network = ResearchNetwork(str(SCENE / "east_nanjing.net.xml"))

    def test_configuration_uses_current_topology_and_monument_behavior(self):
        catalog = HotspotCatalog(self.network, self.spec.config_path)
        square = catalog.hotspots["chen_yi_square"]
        self.assertEqual({"chen_yi_square"}, set(catalog.hotspots))
        self.assertEqual(CORE, set(square["target_edges"]))
        self.assertEqual(CORE, set(square["measurement_edges"]))
        self.assertEqual(ENTRIES, set(square["entry_edges"]))
        self.assertEqual(DESTINATIONS, set(square["visitor_destination_edges"]))
        ordinary = {edge_id for edge_id in self.network.edges if not edge_id.startswith(":")}
        self.assertEqual(ordinary, set(square["visitor_spawn_edges"]))
        self.assertEqual(31, len(ordinary))
        monument = next(item for item in json.loads((ROOT / "config/crowd_hotspots.json").read_text())["hotspots"]
                        if item["id"] == "people_heroes_monument")
        configured_square = json.loads(self.spec.config_path.read_text())["hotspots"][0]
        for field in ("spawn_distribution", "spawn_position_margin_meters", "destination_distribution",
                      "destination_position_margin_meters", "visitor_departure_window_seconds",
                      "activity_window_seconds", "visitor_release_delay_seconds", "visitor_count",
                      "background_count", "route_choice", "phase_thresholds"):
            self.assertEqual(monument[field], configured_square[field], field)

    def test_reproducible_initial_crowd_has_legal_inbound_and_outbound_routes(self):
        router = PositionAwarePedestrianRouter(self.network)
        with tempfile.TemporaryDirectory() as directory:
            first, report = self.spec.generate(Path(directory) / "a", SCENE / "east_nanjing.net.xml", 700)
            second, _ = self.spec.generate(Path(directory) / "b", SCENE / "east_nanjing.net.xml", 700)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            people = ET.parse(first).getroot().findall("person")
        self.assertEqual(700, len(people))
        self.assertTrue(report["initial_population"])
        self.assertEqual([0, 0], report["planned_departure_window_seconds"])
        self.assertEqual([600, 800], report["activity_window_seconds"])
        self.assertEqual([800, 890], report["visitor_release_window_seconds"])
        self.assertEqual("common_event_release_hold", report["dwell_model"])
        self.assertEqual({edge: 140 for edge in DESTINATIONS}, report["destination_edge_counts"])
        spawns = Counter()
        for person in people:
            self.assertEqual("0.00", person.get("depart"))
            self.assertEqual([], person.findall("stop"))
            self.assertEqual(2, len(person.findall("walk")))
            params = {row.get("key"): row.get("value") for row in person.findall("param")}
            self.assertEqual("chen_yi_square", params[HOTSPOT_ID_PARAM])
            self.assertTrue(800 <= float(params[HOTSPOT_RELEASE_TIME_PARAM]) <= 890)
            inbound, outbound = person.findall("walk")
            first_edges, second_edges = (tuple(row.get("edges").split()) for row in (inbound, outbound))
            self.assertEqual(first_edges[-1], second_edges[0])
            self.assertEqual(params[HOTSPOT_TARGET_EDGE_PARAM], first_edges[-1])
            self.assertIn(first_edges[-1], CORE)
            self.assertIn(second_edges[-1], DESTINATIONS)
            self.assertEqual(set(), (set(first_edges) | set(second_edges)) - set(self.network.edges))
            start = EdgePosition(first_edges[0], float(person.get("departPos")))
            target = EdgePosition(first_edges[-1], float(inbound.get("arrivalPos")))
            end = EdgePosition(second_edges[-1], float(outbound.get("arrivalPos")))
            self.assertIsNotNone(router.route(start, target))
            self.assertEqual(second_edges, router.route(target, end).edges)
            spawns[first_edges[0]] += 1
        total_length = sum(self.network.edges[edge].getLength() for edge in report["spawn_edge_counts"])
        for edge, count in report["spawn_edge_counts"].items():
            self.assertEqual(count, spawns[edge])
            self.assertLess(abs(count - 700 * self.network.edges[edge].getLength() / total_length), 1)

    def test_zero_count_and_profiles_keep_hotspot_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            empty, report = self.spec.generate(Path(directory) / "empty", SCENE / "east_nanjing.net.xml", 0)
            self.assertEqual([], ET.parse(empty).getroot().findall("person"))
            self.assertEqual(0, report["effective_count"])
            source, _ = self.spec.generate(Path(directory) / "people", SCENE / "east_nanjing.net.xml", 100)
            population = PopulationManager([source])
            population.profile_sampler.configure_distributions(east_requirement()["population"]["distributions"])
            prepared = population.prepare_demand(Path(directory) / "demand.rou.xml")
            self.assertEqual(100, len(ET.parse(prepared).getroot().findall("person")))
            self.assertEqual({"chen_yi_square"}, set(population.hotspot_ids.values()))
            self.assertEqual(100, len(population.hotspot_release_times))
            self.assertEqual(30, sum(profile.crowd_role == "commuter" for profile in population.profiles.values()))

    def test_dynamic_routes_preserve_targets_and_use_the_correct_portal_orientation(self):
        hotspot = HotspotCatalog(self.network, self.spec.config_path).hotspots["chen_yi_square"]
        provider = RouteProvider(self.network, None)
        choice = HotspotRouteChoice(self.network, provider)
        for source in ("R01", "R22", "R31"):
            for target in CORE:
                with self.subTest(source=source, target=target):
                    snapshot = MotionSnapshot("visitor", 100, 0, 0, 0, 0, 1,
                                              source, source + "_0", 5, 0, 0, tc.STAGE_WALKING)
                    candidates = choice.build_candidates(
                        snapshot, AgentProfile("visitor"), AgentState("visitor"),
                        {**hotspot, "target_edge": target, "target_position": 7.25}, {},
                    )
                    self.assertTrue(candidates)
                    for candidate in candidates:
                        self.assertEqual(target, candidate.edges[-1])
                        self.assertEqual(7.25, candidate.arrival_position)
                        self.assertEqual({candidate.entry_edge}, set(candidate.edges) & ENTRIES)
                        provider.validate_edges(candidate.edges)
                    if source == "R31":
                        self.assertEqual({"R25c", "R26c"}, {candidate.entry_edge for candidate in candidates})


class ChenYiSquareRuntimeTests(unittest.TestCase):
    def test_selected_requirement_initialization_reset_and_dwell_use_the_same_hotspot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = RequirementRepository(root / "requirements").create(RequirementSpec.parse(east_requirement()))
            with patch.dict("os.environ", {"CROWDSIM_PERF": "0", "CROWDSIM_SYSTEM_PERF": "0"}), patch(
                "crowdsim.core.simulation_runtime.PROJECT_ROOT", root), patch(
                "crowdsim.core.simulation_runtime.SumoAdapter", DemandSnapshotAdapter), patch(
                "crowdsim.core.simulation_runtime.ExperimentRecorder",
                side_effect=lambda *args, **kwargs: ExperimentRecorder(*args, **kwargs, root=root / "runs")):
                runtime = build_requirement_runtime(parse_args([]), record)
                try:
                    self.assertTrue(runtime.supports_requirement(record))
                    runtime.configure_requirement(record)
                    runtime.configure_demand(100)
                    runtime.initialize()
                    self.assertEqual(RuntimeState.READY, runtime.state)
                    self.assertEqual(100, len(runtime.current.persons))
                    self.assertIsNone(runtime.poi_catalog)
                    self.assertIsNotNone(runtime.hotspot_route_choice)
                    init = runtime.init_frame()
                    self.assertEqual("chen_yi_square", init["scenario"]["active_hotspot_id"])
                    self.assertEqual("陈毅广场", init["scenario"]["active_hotspot_name"])
                    self.assertEqual("generated_hotspot", init["scenario"]["demand_mode"])
                    self.assertEqual(["chen_yi_square"], [item["id"] for item in init["hotspots"]])
                    self.assertEqual(CORE, set(init["metrics"]["hotspot_metrics"]["chen_yi_square"]["core"]["target_edges"]))
                    self.assertEqual(record["requirement_id"], init["requirement"]["requirement_id"])
                    self.assertIsNotNone(init["metrics"]["observations"])
                    person_id, original = next(iter(runtime.current.persons.items()))
                    target = runtime.population.hotspot_target_edges[person_id]
                    arrived = replace(original, time_seconds=100, edge_id=target, lane_id=target + "_0",
                                      lane_position=runtime.population.hotspot_target_positions[person_id],
                                      remaining_stage_count=1)
                    runtime._update_hotspot_activity(SumoStepResult(100, {person_id: arrived}, {}, (), ()))
                    release_time = runtime.population.hotspot_release_times[person_id]
                    self.assertEqual("hotspot_dwelling", runtime.population.states[person_id].activity_state)
                    self.assertEqual(0, runtime.adapter.speeds[person_id])
                    current, recorder = runtime.current, runtime.recorder
                    state, holds = runtime.population.states, runtime.plan_executor.activity_hold_until
                    async def detach_and_attach():
                        server = OverlayServer(runtime, "unused", 0)
                        server.client = CapturingClient()
                        runtime.start()
                        await server._detach_runtime()
                        await server._attach_run({"run_id": runtime.run_id,
                                                  "requirement_id": record["requirement_id"],
                                                  "location_id": runtime.location_id,
                                                  "network_sha256": runtime.network_sha256}, "restore")
                        return server.client.messages
                    messages = asyncio.run(detach_and_attach())
                    self.assertEqual(RuntimeState.PAUSED, runtime.state)
                    self.assertIs(current, runtime.current)
                    self.assertIs(recorder, runtime.recorder)
                    self.assertIs(state, runtime.population.states)
                    self.assertIs(holds, runtime.plan_executor.activity_hold_until)
                    self.assertFalse(recorder.decision_writer.closed)
                    restored_init, restored_frame = messages
                    self.assertTrue(restored_init["resumed"])
                    self.assertEqual(runtime.snapshot_id, restored_init["snapshot_id"])
                    self.assertEqual(current.time_seconds, restored_init["step_seconds"])
                    self.assertEqual(current.time_seconds, restored_frame["step_seconds"])
                    restored_person = next(row for row in restored_frame["pedestrians"] if row["id"] == person_id)
                    self.assertEqual("hotspot_dwelling", restored_person["state"]["activity_state"])
                    self.assertEqual(state[person_id].hotspot_dwell_until, restored_person["state"]["hotspot_dwell_until"])
                    runtime._maintain_hotspot_activity(release_time - .5)
                    self.assertIn(person_id, runtime.plan_executor.activity_hold_until)
                    runtime._maintain_hotspot_activity(release_time)
                    self.assertEqual("hotspot_departing", runtime.population.states[person_id].activity_state)
                    self.assertGreater(runtime.adapter.speeds[person_id], 0)
                    first_run = runtime.run_id
                    runtime.reset()
                    self.assertNotEqual(first_run, runtime.run_id)
                    self.assertEqual(100, runtime.demand_count)
                    self.assertEqual(record["requirement_id"], runtime.requirement_id)
                    self.assertEqual("chen_yi_square", runtime.scenario_diagnostics()["active_hotspot_id"])
                finally:
                    runtime.close()

    def test_empty_preset_still_advertises_the_configured_hotspot(self):
        with patch("crowdsim.infrastructure.sumo_adapter.discover_sumo_binary", return_value="unused"):
            runtime = build_runtime(parse_args(["--scenario", "east-nanjing-road"]))
        self.assertEqual("chen_yi_square", runtime.scenario_diagnostics()["active_hotspot_id"])
        self.assertEqual("generated_hotspot", runtime.scenario_diagnostics()["demand_mode"])
