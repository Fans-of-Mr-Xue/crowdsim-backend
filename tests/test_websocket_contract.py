import asyncio
import json
from pathlib import Path
import unittest

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


if __name__ == "__main__":
    unittest.main()
