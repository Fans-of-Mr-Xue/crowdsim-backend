import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import websockets

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary
from crowdsim.infrastructure.websocket_server import OverlayServer


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "tests" / "scenarios" / "unidirectional_corridor"


class CapturingClient:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


def has_sumo() -> bool:
    try:
        discover_sumo_binary()
        return True
    except FileNotFoundError:
        return False


@unittest.skipUnless(has_sumo(), "real SUMO binary is required")
class WebSocketContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = SimulationRuntime(SCENARIO / "scenario.sumocfg", pedestrian_route_files=[SCENARIO / "demand.rou.xml"])
        self.server = OverlayServer(self.runtime, "127.0.0.1", 0)
        self.client = CapturingClient()
        self.server.client = self.client

    async def asyncTearDown(self):
        if self.server.task:
            self.server.task.cancel()
            try:
                await self.server.task
            except asyncio.CancelledError:
                pass
        self.runtime.close()

    async def test_configure_returns_compatible_init(self):
        await self.server._handle_message(json.dumps({"action": "configure", "speedFactor": 2, "pushFps": 4}))
        message = self.client.messages[-1]
        self.assertEqual("init", message["type"], message)
        self.assertIn("center", message)
        self.assertIn("flood_points", message)
        self.assertEqual("sumo", message["metrics"]["pedestrian_engine"]["backend"])

    async def test_unknown_action_returns_structured_error(self):
        await self.server._handle_message(json.dumps({"action": "magic", "request_id": "r1"}))
        self.assertEqual({"type": "error", "code": "unknown_action", "message": "Unknown action: magic", "request_id": "r1"}, self.client.messages[-1])

    async def test_update_exposes_skill_decision_fields(self):
        await self.server._handle_message(json.dumps({"action": "configure"}))
        self.runtime.start()
        await self.runtime.tick_async()
        await self.server._send(self.runtime.frame())
        update = self.client.messages[-1]
        self.assertEqual("update", update["type"])
        if update["pedestrians"]:
            state = update["pedestrians"][0]["state"]
            self.assertIn("decision_confidence", state)
            self.assertIn("nationality", state)
            self.assertIn("native_language", state)

    async def test_running_count_change_is_rejected(self):
        await self.server._handle_message(json.dumps({"action": "configure"}))
        await self.server._handle_message(json.dumps({"action": "start"}))
        await self.server._handle_message(json.dumps({"action": "configure", "count": 10, "request_id": "r2"}))
        self.assertEqual("count_requires_reset", self.client.messages[-1]["code"])

    async def test_flow_demand_count_change_is_explicitly_rejected(self):
        await self.server._handle_message(json.dumps({"action": "configure"}))
        first_run = self.runtime.run_id
        await self.server._handle_message(json.dumps({"action": "configure", "count": 2}))
        self.assertEqual("unsupported_demand_count", self.client.messages[-1]["code"])
        self.assertEqual(first_run, self.runtime.run_id)

    async def test_fixed_demand_ignores_client_count_and_exposes_timeline(self):
        runtime = SimulationRuntime(
            SCENARIO / "scenario.sumocfg",
            pedestrian_route_files=[SCENARIO / "demand.rou.xml"],
            scenario_name="hotspot",
            demand_mode="fixed",
            timeline_end_seconds=1200,
        )
        server = OverlayServer(runtime, "127.0.0.1", 0)
        client = CapturingClient()
        server.client = client
        try:
            await server._handle_message(json.dumps({"action": "configure", "count": 8000}))
            init = client.messages[-1]
            self.assertEqual("init", init["type"])
            self.assertEqual("fixed", init["demand"]["mode"])
            self.assertFalse(init["demand"]["count_configurable"])
            self.assertEqual(8000, init["demand"]["requested_count_ignored"])
            self.assertEqual(1200.0, init["scenario"]["timeline_end_seconds"])
            self.assertEqual(2400, init["scenario"]["timeline_step_count"])
            self.assertEqual("READY", init["runtime_state"])
            self.assertIsNone(runtime.demand_count)
            run_id = runtime.run_id
            await server._handle_message(json.dumps({"action": "configure", "count": 9000, "request_id": "fixed-again"}))
            self.assertEqual(run_id, runtime.run_id)
            repeated = next(row for row in reversed(client.messages) if row["type"] == "init")
            self.assertEqual("fixed-again", repeated["request_id"])
            self.assertEqual(9000, repeated["demand"]["requested_count_ignored"])
        finally:
            runtime.close()

    async def test_duplicate_configure_returns_existing_run_and_matches_request(self):
        await self.server._handle_message(json.dumps({"action": "configure", "request_id": "init-1"}))
        run_id = self.runtime.run_id
        await self.server._handle_message(json.dumps({"action": "configure", "request_id": "init-2"}))
        self.assertEqual(run_id, self.runtime.run_id)
        repeated = next(row for row in reversed(self.client.messages) if row["type"] == "init")
        self.assertEqual("init-2", repeated["request_id"])
        self.assertFalse(repeated["demand"]["count_configurable"])
        await self.server._handle_message(json.dumps({"action": "configure", "request_id": "init-2"}))
        self.assertEqual("duplicate_request", self.client.messages[-1]["code"])

    async def test_invalid_count_does_not_initialize_or_change_run(self):
        run_id = self.runtime.run_id
        await self.server._handle_message(json.dumps({"action": "configure", "count": -1, "request_id": "bad"}))
        self.assertEqual("invalid_count", self.client.messages[-1]["code"])
        self.assertEqual("CREATED", self.runtime.state.value)
        self.assertIsNone(self.runtime.recorder)
        self.assertEqual(run_id, self.runtime.run_id)

    async def test_finished_configure_does_not_restart(self):
        await self.server._handle_message(json.dumps({"action": "configure"}))
        run_id = self.runtime.run_id
        from crowdsim.core.simulation_runtime import RuntimeState
        self.runtime.state = RuntimeState.FINISHED
        await self.server._handle_message(json.dumps({"action": "configure", "request_id": "finished"}))
        self.assertEqual(run_id, self.runtime.run_id)
        self.assertEqual("FINISHED", self.client.messages[-1]["runtime_state"])

    async def test_reset_waits_for_old_loop_and_closes_old_writer(self):
        await self.server._handle_message(json.dumps({"action": "configure"}))
        old = self.runtime.recorder
        stopped = []

        async def old_loop():
            try:
                await self.server._loop_stop.wait()
            finally:
                stopped.append(True)

        self.server.task = asyncio.create_task(old_loop())
        await asyncio.sleep(0)
        reset = self.runtime.reset

        def checked_reset(count):
            self.assertEqual([True], stopped)
            self.assertIsNone(self.server.task)
            return reset(count)

        with patch.object(self.runtime, "reset", side_effect=checked_reset):
            await self.server._handle_message(json.dumps({"action": "reset", "request_id": "reset-1"}))
        self.assertTrue(old.decision_writer.closed)
        self.assertNotEqual(old.directory, self.runtime.recorder.directory)
        self.assertEqual("reset-1", self.client.messages[-1]["request_id"])

    async def test_detach_failure_still_releases_client_and_retains_requests(self):
        class EmptyClient(CapturingClient):
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        self.server.client = None
        self.server.processed_request_ids.add("old")
        with patch.object(self.runtime.performance, "flush", side_effect=OSError("forced flush failure")):
            with self.assertRaises(OSError):
                await self.server._handler(EmptyClient())
        self.assertIsNone(self.server.client)
        self.assertEqual({"old"}, self.server.processed_request_ids)

    async def test_real_socket_reconnect_attaches_same_run_and_keeps_writer(self):
        self.server.client = None
        async def receive_init(socket):
            while True:
                message = json.loads(await socket.recv())
                if message["type"] == "init":
                    return message
        async with websockets.serve(self.server._handler, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            async with websockets.connect(url) as socket:
                await socket.send(json.dumps({"action": "configure", "request_id": "connection-init"}))
                first = await receive_init(socket)
                self.assertEqual("init", first["type"])
                old_writer = self.runtime.recorder.decision_writer
                await socket.send(json.dumps({"action": "configure", "request_id": "second-init"}))
                repeated = await receive_init(socket)
                self.assertEqual(first["run_id"], repeated["run_id"])

            async def wait_release():
                while self.server.client is not None:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_release(), 3)
            self.assertFalse(old_writer.closed)
            async with websockets.connect(url) as socket:
                await socket.send(json.dumps({"action": "attach_run", "request_id": "connection-attach",
                                              "run_id": first["run_id"], "requirement_id": None}))
                second = await receive_init(socket)
                self.assertEqual("init", second["type"], second)
                self.assertEqual(first["run_id"], second["run_id"])
                self.assertTrue(second["resumed"])
                self.assertEqual(0, self.runtime.snapshot_index)

    async def test_non_object_message_returns_error(self):
        await self.server._handle_message("[]")
        self.assertEqual("invalid_message", self.client.messages[-1]["code"])


if __name__ == "__main__":
    unittest.main()
