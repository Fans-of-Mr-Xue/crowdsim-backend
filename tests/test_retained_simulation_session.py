"""Retained-session protocol checks using in-memory transports and a fake runtime."""

import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from crowdsim.core.simulation_runtime import RuntimeState
from crowdsim.infrastructure.websocket_server import OverlayServer


class Client:
    def __init__(self):
        self.messages = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def send(self, message):
        self.messages.append(json.loads(message))


class Runtime:
    def __init__(self):
        self.run_id = "run-original"
        self.requirement_id = "req-original"
        self.requirement_record = self.record(self.requirement_id)
        self.location_id = "east-nanjing-road"
        self.network_sha256 = "network-original"
        self.state = RuntimeState.READY
        self.time_seconds = 42.5
        self.real_step_interval = 30
        self.sim_speed_factor = 1
        self.step_length = .5
        self.demand_mode = "configurable"
        self.demand_count = 2
        self.demand_count_configurable = True
        self.current = {"positions": [[1, 2], [3, 4]]}
        self.population = SimpleNamespace(ledger=SimpleNamespace(planned_ids={"p1", "p2"}))
        self.hotspot_state = {"p1": "dwelling"}
        self.recorder = Mock()
        self.performance = Mock()
        self.performance.measure.side_effect = lambda _: nullcontext()
        self.pending = []
        self.results = []
        self.applied = []
        self.closed = 0
        self.resets = 0
        self.ticks = 0
        self.tick_started = asyncio.Event()
        self.tick_release = asyncio.Event()

    @staticmethod
    def record(requirement_id):
        return {"requirement_id": requirement_id, "capabilities": {"location": "supported"},
                "requirement": {"population": {"total": 2},
                                "spatial_scope": {"location_id": "east-nanjing-road"}}}

    def init_frame(self):
        return {"type": "init", "run_id": self.run_id, "runtime_state": self.state.value,
                "step_seconds": self.time_seconds, "requirement": {"requirement_id": self.requirement_id},
                "scenario": {"location_id": self.location_id, "network_sha256": self.network_sha256}}

    def frame(self):
        return {"type": "update", "run_id": self.run_id, "runtime_state": self.state.value,
                "step_seconds": self.time_seconds, "pedestrians": self.current["positions"],
                "event_state": {"policy": list(self.applied)}, "hotspot": dict(self.hotspot_state)}

    async def tick_async(self):
        self.tick_started.set()
        await self.tick_release.wait()
        self.ticks += 1
        self.time_seconds += .5
        self.process_pending_commands()

    def pause(self):
        self.state = RuntimeState.PAUSED

    def start(self):
        self.state = RuntimeState.RUNNING

    def close(self):
        self.closed += 1
        self.state = RuntimeState.CLOSED

    def reset(self, count=None, **_):
        self.resets += 1
        self.run_id = f"run-reset-{self.resets}"
        self.time_seconds = .5
        self.demand_count = count or 2
        self.applied.clear()
        self.state = RuntimeState.READY

    def supports_requirement(self, _):
        return True

    def configure_requirement(self, record):
        self.requirement_record = record
        self.requirement_id = record["requirement_id"]

    def set_playback(self, **_):
        pass

    def queue_command(self, action, data, request_id):
        self.pending.append((action, data, request_id))
        return {"type": "command_result", "status": "queued", "request_id": request_id}

    def process_pending_commands(self):
        for action, data, request_id in self.pending:
            self.applied.append(data["decision"])
            self.results.append({"type": "command_result", "action": action,
                                 "request_id": request_id, "status": "applied"})
        self.pending.clear()

    def take_command_results(self):
        results, self.results = self.results, []
        return results


class RetainedSimulationSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = Runtime()
        self.server = OverlayServer(self.runtime, "unused", 0)
        self.client = Client()
        self.server.client = self.client

    async def command(self, action, request_id="request", **fields):
        await self.server._handle_message(json.dumps({"action": action, "request_id": request_id, **fields}))

    async def attach(self, **fields):
        config = {"run_id": self.runtime.run_id, "requirement_id": self.runtime.requirement_id,
                  "location_id": self.runtime.location_id, "network_sha256": self.runtime.network_sha256}
        config.update(fields)
        await self.command("attach_run", request_id="attach", **config)

    async def test_disconnect_retains_engine_writer_population_and_request_scope(self):
        self.runtime.start()
        current, recorder, hotspot = self.runtime.current, self.runtime.recorder, self.runtime.hotspot_state
        self.server.processed_request_ids.add("already-applied")
        self.server.client = None
        await self.server._handler(self.client)
        self.assertEqual(RuntimeState.PAUSED, self.runtime.state)
        self.assertEqual(42.5, self.runtime.time_seconds)
        self.assertIs(current, self.runtime.current)
        self.assertIs(recorder, self.runtime.recorder)
        self.assertIs(hotspot, self.runtime.hotspot_state)
        self.assertEqual(0, self.runtime.closed)
        self.assertEqual({"already-applied"}, self.server.processed_request_ids)
        self.assertIsNone(self.server.client)

    async def test_detach_finishes_inflight_tick_without_cancel_or_next_tick(self):
        self.runtime.start()
        self.server.task = asyncio.create_task(self.server._run_loop())
        await self.runtime.tick_started.wait()
        detach = asyncio.create_task(self.server._detach_runtime())
        await asyncio.sleep(0)
        self.assertFalse(detach.done())
        self.runtime.tick_release.set()
        await asyncio.wait_for(detach, 1)
        self.assertEqual(1, self.runtime.ticks)
        self.assertEqual(43, self.runtime.time_seconds)
        self.assertEqual(RuntimeState.PAUSED, self.runtime.state)
        self.assertIsNone(self.server.task)
        self.assertEqual(0, self.runtime.closed)

    async def test_attach_returns_authoritative_time_positions_events_and_hold_state(self):
        self.runtime.start()
        await self.attach()
        init, frame = self.client.messages
        self.assertTrue(init["resumed"])
        self.assertEqual(1, init["session_protocol_version"])
        self.assertEqual(42.5, frame["step_seconds"])
        self.assertEqual(self.runtime.current["positions"], frame["pedestrians"])
        self.assertEqual(self.runtime.hotspot_state, frame["hotspot"])
        self.assertEqual("PAUSED", frame["runtime_state"])
        self.assertEqual(0, self.runtime.resets)

    async def test_finished_attach_keeps_finished_state(self):
        self.runtime.state = RuntimeState.FINISHED
        await self.attach()
        self.assertEqual("FINISHED", self.client.messages[-1]["runtime_state"])

    async def test_attach_rejects_missing_or_stale_run_and_keeps_existing_run(self):
        for run_id in (None, "old-run"):
            await self.attach(run_id=run_id)
            error = self.client.messages[-1]
            self.assertEqual("run_not_found", error["code"])
            self.assertEqual(self.runtime.run_id, error["active_run"]["run_id"])
        self.assertEqual(0, self.runtime.resets)

    async def test_attach_rejects_other_requirement_or_network(self):
        for fields, code in (({"requirement_id": "another"}, "run_requirement_mismatch"),
                             ({"location_id": "memorial-tower"}, "run_scenario_mismatch"),
                             ({"network_sha256": "another"}, "run_scenario_mismatch")):
            await self.attach(**fields)
            self.assertEqual(code, self.client.messages[-1]["code"])
        self.assertEqual(RuntimeState.READY, self.runtime.state)

    async def test_closed_run_cannot_attach(self):
        self.runtime.close()
        await self.attach()
        self.assertEqual("run_unavailable", self.client.messages[-1]["code"])

    async def test_policy_replay_after_detach_applies_only_once(self):
        await self.command("event_decision", decision="divert", run_id=self.runtime.run_id)
        await self.server._detach_runtime()
        self.server.client = Client()
        await self.command("event_decision", decision="divert", run_id=self.runtime.run_id)
        self.assertEqual(["divert"], self.runtime.applied)
        self.assertEqual("applied", self.server.client.messages[-1]["status"])
        await self.command("event_decision", decision="divert", run_id="another-run")
        self.assertEqual("run_not_found", self.server.client.messages[-1]["code"])

    async def test_lost_ack_is_cached_before_transport_failure(self):
        class FailedClient(Client):
            async def send(self, _):
                raise OSError("transport closed")
        self.server.client = FailedClient()
        with self.assertRaises(OSError):
            await self.command("event_decision", decision="divert", run_id=self.runtime.run_id)
        self.server.client = Client()
        await self.command("event_decision", decision="divert", run_id=self.runtime.run_id)
        self.assertEqual(["divert"], self.runtime.applied)
        self.assertEqual("applied", self.server.client.messages[-1]["status"])

    async def test_explicit_reset_replaces_run_and_clears_dedup_scope(self):
        await self.command("event_decision", "policy", decision="divert", run_id=self.runtime.run_id)
        old_run = self.runtime.run_id
        self.server.requirements = SimpleNamespace(load=lambda _: self.runtime.record("req-new"))
        await self.command("reset", "reset", run_id=old_run, requirement_id="req-new")
        self.assertNotEqual(old_run, self.runtime.run_id)
        self.assertEqual("req-new", self.runtime.requirement_id)
        self.assertEqual(1, self.runtime.closed)
        self.assertFalse(self.client.messages[-1]["resumed"])
        self.assertNotIn("policy", self.server.request_results)
        await self.command("event_decision", "policy", decision="guide", run_id=self.runtime.run_id)
        self.assertEqual(["guide"], self.runtime.applied)

    async def test_invalid_reset_does_not_destroy_retained_run(self):
        self.server.requirements = SimpleNamespace(load=lambda _: self.runtime.record("req-new"))
        await self.command("reset", run_id=self.runtime.run_id, requirement_id="req-new", count=3)
        self.assertEqual("requirement_count_mismatch", self.client.messages[-1]["code"])
        self.assertEqual(0, self.runtime.closed)
        self.assertEqual("run-original", self.runtime.run_id)

    async def test_explicit_close_destroys_runtime_and_rejects_later_attach(self):
        await self.command("close_run", run_id=self.runtime.run_id)
        self.assertEqual("run_closed", self.client.messages[-1]["type"])
        self.assertEqual(RuntimeState.CLOSED, self.runtime.state)
        await self.attach()
        self.assertEqual("run_unavailable", self.client.messages[-1]["code"])

    async def test_failure_while_detaching_still_releases_client(self):
        self.server.client = None
        self.runtime.performance.flush.side_effect = OSError("flush failed")
        with self.assertRaises(OSError):
            await self.server._handler(self.client)
        self.assertIsNone(self.server.client)
        self.assertEqual(0, self.runtime.closed)
