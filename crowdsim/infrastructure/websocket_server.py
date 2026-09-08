"""Single-client WebSocket adapter for :class:`SimulationRuntime`."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
import json
from typing import Any, Dict, Optional

import websockets

from crowdsim.core.simulation_runtime import RuntimeState


class OverlayServer:
    def __init__(self, simulator: Any, host: str, port: int) -> None:
        self.simulator = simulator
        self.host = host
        self.port = port
        self.client: Any = None
        self.task: Optional[asyncio.Task] = None
        self.processed_request_ids: set[str] = set()

    async def start(self) -> None:
        server = await websockets.serve(self._handler, self.host, self.port)
        print(f"[CrowdSim] WebSocket listening at ws://{self.host}:{self.port}")
        await server.wait_closed()

    async def _handler(self, websocket: Any) -> None:
        if self.client is not None:
            await websocket.send(json.dumps(self._error("single_client_only", "Only one client is supported.")))
            await websocket.close()
            return
        self.client = websocket
        try:
            async for message in websocket:
                await self._handle_message(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self.task is not None:
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task
                self.task = None
            self.simulator.close()
            self.client = None

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self._send(self._error("invalid_json", "Message must be valid JSON."))
            return
        action = data.get("action")
        request_id = data.get("request_id")
        if request_id is not None and str(request_id) in self.processed_request_ids:
            await self._send(self._error("duplicate_request", "request_id was already processed", request_id))
            return
        try:
            if action == "configure":
                if data.get("count") is not None:
                    if self.simulator.state == RuntimeState.READY and self.simulator.snapshot_index == 0:
                        if not self.simulator.population.ledger.planned_ids:
                            raise CommandError("unsupported_demand_count", "count requires explicit <person> demand")
                        self.simulator.reset(data.get("count"))
                    else:
                        try:
                            self.simulator.configure_demand(data.get("count"))
                        except RuntimeError as exc:
                            raise CommandError("count_requires_reset", str(exc)) from exc
                self.simulator.set_playback(speed_factor=data.get("speedFactor"), push_fps=data.get("pushFps"))
                if self.simulator.state == RuntimeState.CREATED:
                    self.simulator.initialize()
                await self._send(self.simulator.init_frame())
            elif action == "set_speed":
                self.simulator.set_playback(speed_factor=data.get("speedFactor"))
                await self._send({"type": "speed", "request_id": request_id, "speedFactor": self.simulator.sim_speed_factor, "step_length": self.simulator.step_length, "real_step_interval": self.simulator.real_step_interval})
            elif action == "start":
                if self.simulator.state == RuntimeState.CREATED:
                    self.simulator.initialize()
                self.simulator.start()
                await self._command_result(action, request_id, "applied")
                if self.task is None or self.task.done():
                    self.task = asyncio.create_task(self._run_loop())
            elif action == "pause":
                self.simulator.pause()
                await self._command_result(action, request_id, "applied")
            elif action == "get_status":
                await self._send({"type": "status", "request_id": request_id, **self.simulator.diagnostics()})
            elif action == "get_agent_state":
                person_id = str(data.get("id", ""))
                state = self.simulator.population.state_for(person_id)
                motion = self.simulator.current.persons.get(person_id) if self.simulator.current else None
                if state is None and motion is None:
                    raise CommandError("unknown_agent", f"Unknown person id: {person_id}")
                await self._send({"type": "agent_state", "request_id": request_id, "snapshot_id": self.simulator.snapshot_id, "time": self.simulator.time_seconds, "id": person_id, "profile": asdict(self.simulator.profile_for(person_id)), "state": asdict(state) if state else None, "motion": asdict(motion) if motion else None})
            elif action in {"update_flood_source", "set_event", "trigger_event", "event_decision", "set_policy", "apply_policy", "set_group"}:
                queued = self.simulator.queue_command(action, data, request_id)
                if self.simulator.state != RuntimeState.RUNNING:
                    self.simulator.process_pending_commands()
                    for result in self.simulator.take_command_results():
                        await self._send(result)
                else:
                    await self._send(queued)
            elif action == "reset":
                self.simulator.reset(data.get("count"))
                await self._send(self.simulator.init_frame())
            else:
                raise CommandError("unknown_action", f"Unknown action: {action}")
            if request_id is not None:
                self.processed_request_ids.add(str(request_id))
        except CommandError as exc:
            await self._send(self._error(exc.code, str(exc), request_id))
        except (RuntimeError, ValueError) as exc:
            await self._send(self._error("invalid_state", str(exc), request_id))

    async def _run_loop(self) -> None:
        while self.client is not None:
            if self.simulator.state == RuntimeState.RUNNING:
                await self.simulator.tick_async()
                for result in self.simulator.take_command_results():
                    await self._send(result)
                await self._send(self.simulator.frame())
            if self.simulator.state in {RuntimeState.FINISHED, RuntimeState.ERROR, RuntimeState.CLOSED}:
                return
            await asyncio.sleep(self.simulator.real_step_interval)

    async def _command_result(self, action: str, request_id: Any, status: str) -> None:
        result = {"type": "command_result", "request_id": request_id, "action": action, "status": status, "applied_at": self.simulator.time_seconds}
        if self.simulator.recorder is not None:
            self.simulator.recorder.record_command({"action": action, "request_id": request_id}, result)
        await self._send(result)

    @staticmethod
    def _error(code: str, message: str, request_id: Any = None) -> Dict[str, Any]:
        return {"type": "error", "code": code, "message": message, "request_id": request_id}

    async def _send(self, payload: Dict[str, Any]) -> None:
        if self.client is not None:
            await self.client.send(json.dumps(payload, default=str))


class CommandError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
