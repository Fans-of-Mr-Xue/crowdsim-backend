import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import websockets

from crowdsim.core.simulation_runtime import RuntimeState
from crowdsim.core.population_manager import PopulationManager
from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter
from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.domain.crowdsim_models import MotionSnapshot
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.sumo_adapter import configure_sumo_projection, discover_sumo_binary
from crowdsim.infrastructure.sumo_adapter import SumoStepResult
from crowdsim.infrastructure.websocket_server import OverlayServer
from crowdsim.scenarios.generated_network_demand import NetworkDemandSpec
from crowdsim_overlay_server import build_requirement_runtime, build_runtime, parse_args
from tests.test_requirement_submission import requirement_payload
from tests.test_websocket_contract import CapturingClient, has_sumo

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "scenarios/east_nanjing_road"


def east_requirement(count=100):
    payload = requirement_payload("east-nanjing-road")
    polygon = json.loads((SCENE / "network_spec.json").read_text())["preset_wgs84_coordinates"]
    if polygon[-1] != polygon[0]:
        polygon.append(polygon[0])
    payload["spatial_scope"].update(name="南京东路外滩路口", location_code="BUND-02",
                                   center=[121.48475, 31.241],
                                   boundary={"type": "Polygon", "coordinates": [polygon]})
    payload["population"]["total"] = count
    return payload


class GeneratedNetworkDemandTests(unittest.TestCase):
    def test_accepted_location_and_reproducible_full_population(self):
        self.assertEqual("supported", RequirementSpec.parse(east_requirement()).capabilities()["location"])
        network = ResearchNetwork(str(SCENE / "east_nanjing.net.xml"))
        router = PositionAwarePedestrianRouter(network)
        with tempfile.TemporaryDirectory() as directory:
            first, report = NetworkDemandSpec().generate(Path(directory) / "a", SCENE / "east_nanjing.net.xml", 700)
            second, _ = NetworkDemandSpec().generate(Path(directory) / "b", SCENE / "east_nanjing.net.xml", 700)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            persons = ET.parse(first).getroot().findall("person")
            self.assertEqual(700, len(persons))
            self.assertEqual(700, sum(report["spawn_allocation"].values()))
            for person in persons:
                self.assertEqual("0", person.get("depart"))
                walk = person.find("walk")
                edges = tuple(walk.get("edges").split())
                self.assertTrue(all(edge in network.edges and not edge.startswith(":") for edge in edges))
                route = router.route(EdgePosition(edges[0], float(person.get("departPos"))),
                                     EdgePosition(edges[-1], float(walk.get("arrivalPos"))))
                self.assertEqual(route.edges, edges)
                self.assertGreaterEqual(route.distance_m, 49.999)

    def test_zero_count_and_count_limits(self):
        spec = NetworkDemandSpec()
        with tempfile.TemporaryDirectory() as directory:
            output, report = spec.generate(directory, SCENE / "east_nanjing.net.xml", 0)
            self.assertEqual([], ET.parse(output).getroot().findall("person"))
            self.assertEqual(0, report["effective_count"])
        for count in [-1, 10001, True, 1.5]:
            with self.subTest(count=count), self.assertRaises(ValueError):
                spec.validate_count(count)


@unittest.skipUnless(has_sumo(), "real SUMO binary is required")
class NetworkDemandSumoCliTests(unittest.TestCase):
    def test_full_700_person_demand_arrives_without_jam_recovery(self):
        binary = discover_sumo_binary()
        configure_sumo_projection(binary)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, _ = NetworkDemandSpec().generate(directory, SCENE / "east_nanjing.net.xml", 700)
            population = PopulationManager([source])
            population.profile_sampler.configure_distributions(east_requirement()["population"]["distributions"])
            demand = population.prepare_demand(directory / "demand.rou.xml")
            result = subprocess.run([binary, "-c", str(SCENE / "east_nanjing.sumocfg"),
                                     "--route-files", str(demand), "--tripinfo-output", str(directory / "trips.xml"),
                                     "--tripinfo-output.write-unfinished", "true"],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(0, result.returncode, result.stderr)
            persons = ET.parse(directory / "trips.xml").getroot().findall("personinfo")
            self.assertEqual(700, len(persons))
            self.assertTrue(all(float(person.find("walk").get("arrival")) > 0 for person in persons))
            self.assertNotIn("jammed", result.stderr.lower())
            print("SUMO CLI: 700/700 persons arrived; no jam recovery; final arrival",
                  max(float(person.find("walk").get("arrival")) for person in persons))

    def test_runtime_initialization_and_frame_use_real_cli_positions_on_the_selected_network(self):
        # Replace only the socket transport. SUMO CLI still executes the actual
        # generated demand; runtime setup, profiles, observations and serialization
        # use its real initial positions rather than hand-written mock persons.
        class OfflineInitialAdapter:
            def __init__(self, config_path, *, sumo_binary=None, extra_args=()):
                self.config_path = config_path
                self.extra_args = list(extra_args)
                self.closed = False
                self.started = False

            @property
            def diagnostics(self):
                return {"backend": "sumo-cli-test", "closed": self.closed}

            def start(self):
                binary = discover_sumo_binary()
                configure_sumo_projection(binary)
                output = Path(self.extra_args[self.extra_args.index("--log") + 1]).parent / "initial.fcd.xml"
                result = subprocess.run([binary, "-c", str(self.config_path), *self.extra_args,
                                         "--end", "0.5", "--fcd-output", str(output)],
                                        capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise RuntimeError(result.stderr)
                network = ResearchNetwork(str(SCENE / "east_nanjing.net.xml"))
                persons = {}
                for row in ET.parse(output).getroot().findall("./timestep/person"):
                    edge_id = row.get("edge")
                    x, y = float(row.get("x")), float(row.get("y"))
                    persons[row.get("id")] = MotionSnapshot(
                        row.get("id"), .5, x, y, *network.xy_to_lonlat(x, y),
                        float(row.get("speed")), edge_id, f"{edge_id}_0",
                        float(row.get("pos")), float(row.get("angle")), 0, 2,
                        # FCD reports positions, not stage metadata. Initial
                        # hotspot demand declares two walks; arrival transitions
                        # are tested separately with the offline runtime adapter.
                        remaining_stage_count=2,
                    )
                self.initial = SumoStepResult(.5, persons, {}, tuple(persons), ())
                self.started = True
                return SumoStepResult(0, {}, {}, (), ())

            def step(self):
                return self.initial

            def display_edge(self, person_id, edge_id):
                return edge_id

            def set_person_speed(self, person_id, speed):
                pass

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"CROWDSIM_PERF": "0", "CROWDSIM_SYSTEM_PERF": "0"}):
            directory = Path(directory)
            record = RequirementRepository(directory / "requirements").create(RequirementSpec.parse(east_requirement()))
            with patch("crowdsim.core.simulation_runtime.PROJECT_ROOT", directory), patch(
                "crowdsim.core.simulation_runtime.ExperimentRecorder",
                side_effect=lambda *args, **kwargs: ExperimentRecorder(*args, **kwargs, root=directory / "runs")), patch(
                "crowdsim.core.simulation_runtime.SumoAdapter", OfflineInitialAdapter):
                runtime = build_requirement_runtime(parse_args([]), record)
                try:
                    runtime.configure_requirement(record)
                    runtime.configure_demand(100)
                    runtime.initialize()
                    self.assertEqual(RuntimeState.READY, runtime.state)
                    self.assertEqual(.5, runtime.time_seconds)
                    self.assertEqual(100, len(runtime.current.persons))
                    frame = runtime.frame()
                    self.assertEqual(100, len(frame["pedestrians"]))
                    self.assertEqual("east-nanjing-road", frame["scenario"]["location_id"])
                    self.assertEqual({"chen_yi_square"}, set(runtime.hotspot_catalog.hotspots))
                    self.assertIsNotNone(runtime.hotspot_route_choice)
                    self.assertEqual("chen_yi_square", frame["scenario"]["active_hotspot_id"])
                    self.assertTrue(runtime.observation_collector is not None)
                    self.assertEqual("east_nanjing.net.xml", Path(runtime.network.net_path).name)
                    self.assertEqual(30, sum(row.crowd_role == "commuter" for row in runtime.population.profiles.values()))
                    self.assertTrue(all(row.edge_id.startswith("R") for row in runtime.current.persons.values()))
                    self.assertEqual(100, runtime.init_frame()["demand"]["planned"])
                finally:
                    runtime.close()


class RequirementScenarioBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_binding_is_atomic_and_reconnect_keeps_the_selected_requirement(self):
        args = parse_args([])
        with tempfile.TemporaryDirectory() as directory, patch(
            "crowdsim.infrastructure.sumo_adapter.discover_sumo_binary", return_value="test-no-sumo"):
            repository = RequirementRepository(directory)
            record = repository.create(RequirementSpec.parse(east_requirement()))
            initial = build_runtime(args)
            server = OverlayServer(initial, "127.0.0.1", 0, requirement_repository=repository,
                                   runtime_factory=lambda value: build_requirement_runtime(args, value))
            client = CapturingClient()
            server.client = client
            initialized = []
            def initialize(runtime):
                initialized.append(runtime.config_path)
                runtime.state = RuntimeState.READY
            def init_frame(runtime):
                return {"type": "init", "run_id": runtime.run_id, "runtime_state": runtime.state.value,
                        "location_id": runtime.location_id, "count": runtime.demand_count}
            try:
                with patch("crowdsim.core.simulation_runtime.SimulationRuntime.initialize", initialize), patch(
                    "crowdsim.core.simulation_runtime.SimulationRuntime.init_frame", init_frame):
                    await server._handle_message(json.dumps({"action": "configure", "requirement_id": record["requirement_id"], "count": 5}))
                    self.assertEqual("requirement_count_mismatch", client.messages[-1]["code"])
                    self.assertIs(initial, server.simulator)
                    self.assertIsNone(initial.requirement_record)
                    await server._handle_message(json.dumps({"action": "configure", "requirement_id": record["requirement_id"]}))
                    self.assertEqual("init", client.messages[-1]["type"], client.messages[-1])
                    self.assertEqual(SCENE / "east_nanjing.sumocfg", initialized[-1])
                    self.assertEqual("east-nanjing-road", server.simulator.location_id)
                    self.assertEqual(100, server.simulator.demand_count)
                    first_run = server.simulator.run_id
                    await server._handle_message(json.dumps({"action": "configure", "count": 5}))
                    self.assertEqual("requirement_count_mismatch", client.messages[-1]["code"])
                    self.assertEqual(first_run, server.simulator.run_id)
                    await server._handle_message(json.dumps({"action": "configure", "requirement_id": record["requirement_id"]}))
                    self.assertEqual(first_run, server.simulator.run_id)
                    self.assertEqual(1, len(initialized))
                    await server._handle_message(json.dumps({"action": "reset"}))
                    self.assertEqual(100, server.simulator.demand_count)
                    self.assertEqual(record["requirement_id"], server.simulator.requirement_id)
                    self.assertEqual("east-nanjing-road", server.simulator.location_id)
                    self.assertNotEqual(first_run, server.simulator.run_id)
                    server.simulator.close()
                    await server._handle_message(json.dumps({"action": "configure", "requirement_id": record["requirement_id"]}))
                    self.assertEqual("init", client.messages[-1]["type"])
                    self.assertEqual(100, server.simulator.demand_count)
            finally:
                await server._stop_runtime()


@unittest.skipUnless(has_sumo(), "real SUMO binary is required")
class RequirementScenarioWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_connect_start_pause_reset_and_reconnect_other_location(self):
        args = parse_args([])  # Standard research launch must accept both presets.
        with tempfile.TemporaryDirectory() as directory:
            server = OverlayServer(build_runtime(args), "127.0.0.1", 0,
                                   requirement_repository=RequirementRepository(directory),
                                   runtime_factory=lambda record: build_requirement_runtime(args, record))
            async def receive(socket, kind, request_id=None):
                while True:
                    message = json.loads(await asyncio.wait_for(socket.recv(), 30))
                    if message["type"] == "error":
                        return message
                    if message["type"] == kind and (request_id is None or message.get("request_id") == request_id):
                        return message

            async def send(socket, action, request_id, **kwargs):
                await socket.send(json.dumps(dict(action=action, request_id=request_id, **kwargs)))

            try:
                async with websockets.serve(server._handler, "127.0.0.1", 0) as listener:
                    url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
                    async with websockets.connect(url) as socket:
                        await send(socket, "submit_requirement", "east-submit", requirement=east_requirement())
                        accepted = await receive(socket, "requirement_accepted")
                        self.assertEqual("supported", accepted["capabilities"]["location"])
                        east_id = accepted["requirement_id"]
                        await send(socket, "configure", "east-config", requirement_id=east_id, speedFactor=20)
                        initial = await receive(socket, "init", "east-config")
                        self.assertEqual("east-nanjing-road", initial["scenario"]["location_id"], initial)
                        self.assertEqual("generated_hotspot", initial["demand"]["mode"])
                        self.assertEqual(100, initial["demand"]["planned"])
                        ready = await receive(socket, "update")
                        self.assertEqual("READY", ready["runtime_state"])
                        self.assertEqual(100, len(ready["pedestrians"]))
                        self.assertEqual(100, len(server.simulator.current.persons))
                        self.assertTrue(all(121.48 < row.lon < 121.49 and 31.23 < row.lat < 31.25
                                            for row in server.simulator.current.persons.values()))
                        profiles = list(server.simulator.population.profiles.values())
                        self.assertEqual(30, sum(row.crowd_role == "commuter" for row in profiles))
                        self.assertEqual(50, sum(row.gender == "female" for row in profiles))
                        self.assertEqual(60, sum(row.origin == "本地居民" for row in profiles))
                        before = {key: (row.x, row.y) for key, row in server.simulator.current.persons.items()}
                        await send(socket, "configure", "same-config", requirement_id=east_id)
                        same = await receive(socket, "init", "same-config")
                        self.assertEqual(initial["run_id"], same["run_id"])
                        await send(socket, "start", "start-1")
                        started = await receive(socket, "command_result", "start-1")
                        self.assertEqual("applied", started["status"])
                        moving = await receive(socket, "update")
                        self.assertEqual("RUNNING", moving["runtime_state"])
                        self.assertTrue(any((row.x, row.y) != before[key]
                                            for key, row in server.simulator.current.persons.items()))
                        await send(socket, "pause", "pause-1")
                        await receive(socket, "command_result", "pause-1")
                        frozen = server.simulator.time_seconds
                        await asyncio.sleep(.1)
                        self.assertEqual(frozen, server.simulator.time_seconds)
                        await send(socket, "reset", "reset-1")
                        reset = await receive(socket, "init", "reset-1")
                        self.assertNotEqual(initial["run_id"], reset["run_id"])
                        self.assertEqual(100, reset["demand"]["planned"])
                        self.assertEqual("east-nanjing-road", reset["scenario"]["location_id"])
                        await send(socket, "submit_requirement", "memorial-submit", requirement=requirement_payload())
                        memorial = await receive(socket, "requirement_accepted")
                        await send(socket, "configure", "unsafe-switch", requirement_id=memorial["requirement_id"])
                        error = await receive(socket, "init", "unsafe-switch")
                        self.assertEqual("requirement_requires_reset", error["code"])
                        self.assertEqual(reset["run_id"], server.simulator.run_id)
                    for _ in range(100):
                        if server.client is None:
                            break
                        await asyncio.sleep(.02)
                    self.assertEqual(RuntimeState.READY, server.simulator.state)
                    async with websockets.connect(url) as socket:
                        await send(socket, "reset", "memorial-config", run_id=reset["run_id"], requirement_id=memorial["requirement_id"])
                        other = await receive(socket, "init", "memorial-config")
                        self.assertEqual("memorial-tower", other["scenario"]["location_id"], other)
                        self.assertEqual("generated_hotspot", other["demand"]["mode"])
                        self.assertEqual(10, other["demand"]["planned"])
                        self.assertNotEqual(reset["run_id"], other["run_id"])
                        await send(socket, "start", "memorial-start")
                        await receive(socket, "command_result", "memorial-start")
                        self.assertEqual("RUNNING", (await receive(socket, "update"))["runtime_state"])
            finally:
                await server._stop_runtime()


if __name__ == "__main__":
    unittest.main()
