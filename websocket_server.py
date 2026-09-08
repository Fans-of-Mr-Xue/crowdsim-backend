import asyncio
import json
from typing import Any, Dict, Optional

import websockets


class OverlayServer:
    def __init__(self, simulator: Any, host: str, port: int) -> None:
        self.simulator = simulator
        self.host = host
        self.port = port
        self.client: Any = None
        self.task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        server = await websockets.serve(self._handler, self.host, self.port)
        print(f"[OverlaySim] WebSocket listening at ws://{self.host}:{self.port}")
        print(f"[OverlaySim] center={self.simulator.center}")
        await server.wait_closed()

    async def _handler(self, websocket: Any) -> None:
        if self.client is not None:
            await websocket.send(json.dumps({"type": "error", "message": "Only one client is supported."}))
            await websocket.close()
            return
        self.client = websocket
        print("[OverlaySim] Frontend connected")
        try:
            async for message in websocket:
                await self._handle_message(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.simulator.running = False
            self.client = None
            if self.task is not None:
                self.task.cancel()
                self.task = None
            print("[OverlaySim] Frontend disconnected")

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        action = data.get("action")
        if action == "configure":
            self.simulator.configure(data.get("count"), data.get("speedFactor"), data.get("pushFps"))
            await self._send_init()
        elif action == "set_speed":
            self.simulator.configure(speed_factor=data.get("speedFactor"))
            await self._send({
                "type": "speed",
                "speedFactor": self.simulator.sim_speed_factor,
                "step_length": self.simulator.step_length,
                "real_step_interval": self.simulator.step_length / max(0.01, self.simulator.sim_speed_factor),
            })
        elif action == "start":
            self.simulator.running = True
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._run_loop())
        elif action == "pause":
            self.simulator.running = False
        elif action == "update_flood_source":
            self.simulator.set_flood(data)
            await self._send(self.simulator.frame())
        elif action in {"set_event", "trigger_event"}:
            self.simulator.set_event(data)
            await self._send(self.simulator.frame())
        elif action in {"event_decision", "set_policy", "apply_policy"}:
            self.simulator.apply_policy(data)
            await self._send(self.simulator.frame())

    async def _send_init(self) -> None:
        center_lat, center_lon = self.simulator.center
        await self._send({
            "type": "init",
            "center": [center_lat, center_lon],
            "speedFactor": self.simulator.sim_speed_factor,
            "step_length": self.simulator.step_length,
            "real_step_interval": self.simulator.step_length / max(0.01, self.simulator.sim_speed_factor),
            "flood_points": [
                {"lng": zone.lon, "lat": zone.lat, "depth": zone.depth, "radius": zone.radius}
                for zone in self.simulator.flood_zones
            ],
            "flooded_roads": self.simulator._last_flooded_roads,
            "metrics": {
                "pedestrian_engine": self.simulator.ped_ltm.diagnostics,
                "decision_engine": self.simulator.decision_engine.diagnostics(),
            },
        })

    async def _run_loop(self) -> None:
        while self.client is not None:
            if self.simulator.running:
                self.simulator.step()
                await self.simulator.resolve_agent_decisions()
                await self._send(self.simulator.frame())
            await asyncio.sleep(self.simulator.step_length / max(0.01, self.simulator.sim_speed_factor))

    async def _send(self, payload: Dict[str, Any]) -> None:
        if self.client is not None:
            await self.client.send(json.dumps(payload))
