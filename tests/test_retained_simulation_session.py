"""Retained-session protocol checks using in-memory transports and a fake runtime."""

import asyncio
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
from crowdsim.infrastructure.websocket_server import OverlayServer
from tests.test_websocket_contract import has_sumo


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
        self.artificial_society_record = None
        self.artificial_society_id = None
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
        model = getattr(self, "artificial_society_record", None)
        return {"type": "init", "run_id": self.run_id, "runtime_state": self.state.value,
                "step_seconds": self.time_seconds, "requirement": {"requirement_id": self.requirement_id},
                "artificial_society": ({"model_id": model.get("model_id"), "fingerprint": model.get("fingerprint")}
                                       if isinstance(model, dict) else None),
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


class GuardedRuntime(Runtime):
    """Use the production binding guard instead of the permissive session fake."""

    def __init__(self):
        super().__init__()
        self.population.profile_sampler = Mock()
        self.artificial_society_record = None
        self.artificial_society_id = None
        self.events = []

    @staticmethod
    def record(requirement_id):
        result = Runtime.record(requirement_id)
        result["requirement"]["population"]["distributions"] = {}
        return result

    def configure_requirement(self, record):
        SimulationRuntime.configure_requirement(self, record)
        self.events.append(("bind_requirement", self.state, self.requirement_id))

    def configure_artificial_society(self, record):
        if record["requirement_id"] != self.requirement_id:
            raise ValueError("artificial society model does not match configured requirement")
        self.artificial_society_record = record
        self.artificial_society_id = record["model_id"]
        self.events.append(("bind_model", self.state, self.requirement_id))

    def close(self):
        self.events.append(("close", self.state, self.requirement_id))
        super().close()

    def reset(self, count=None, **kwargs):
        # Production reset re-applies the saved model after binding the requirement.
        if self.artificial_society_record is not None:
            self.configure_artificial_society(self.artificial_society_record)
        self.events.append(("reset", self.state, self.requirement_id))
        super().reset(count, **kwargs)


class ResetBindingOrderTests(unittest.IsolatedAsyncioTestCase):
    def make_server(self, state=RuntimeState.PAUSED):
        runtime = GuardedRuntime()
        runtime.state = state
        server = OverlayServer(runtime, "unused", 0)
        server.client = Client()
        server.requirements = SimpleNamespace(load=runtime.record)
        server.artificial_societies = SimpleNamespace(load=lambda model_id: {
            "model_id": model_id, "requirement_id": "req-new", "fingerprint": f"fp-{model_id}",
        })
        return runtime, server

    async def reset(self, server, **fields):
        await server._handle_message(json.dumps({
            "action": "reset", "request_id": "reset-new", "run_id": server.simulator.run_id,
            "requirement_id": "req-new", **fields,
        }))

    async def test_reset_closes_before_binding_in_all_retained_states(self):
        for state in (RuntimeState.READY, RuntimeState.RUNNING, RuntimeState.PAUSED, RuntimeState.FINISHED):
            with self.subTest(state=state.value):
                runtime, server = self.make_server(state)
                await self.reset(server, artificial_society_id="model-new")
                self.assertEqual("init", server.client.messages[-1]["type"], server.client.messages)
                self.assertEqual("req-new", runtime.requirement_id)
                self.assertEqual("model-new", runtime.artificial_society_id)
                self.assertEqual("close", runtime.events[0][0])
                self.assertTrue(all(event[1] == RuntimeState.CLOSED for event in runtime.events
                                    if event[0].startswith("bind_")))
                self.assertEqual(1, runtime.resets)

    async def test_reset_waits_for_inflight_tick_before_binding(self):
        runtime, server = self.make_server(RuntimeState.RUNNING)
        server.task = asyncio.create_task(server._run_loop())
        await asyncio.wait_for(runtime.tick_started.wait(), 1)
        resetting = asyncio.create_task(self.reset(server, artificial_society_id="model-new"))
        try:
            await asyncio.sleep(0)
            self.assertFalse(resetting.done())
            self.assertEqual("req-original", runtime.requirement_id)
            self.assertEqual([], runtime.events)
        finally:
            runtime.tick_release.set()
            await asyncio.wait_for(resetting, 2)
            await server._stop_loop()
        self.assertEqual(1, runtime.ticks)
        self.assertEqual("init", server.client.messages[-1]["type"], server.client.messages)

    async def test_invalid_reset_preserves_old_binding_and_running_state(self):
        runtime, server = self.make_server(RuntimeState.RUNNING)
        original = runtime.requirement_record
        await self.reset(server, count=3)
        self.assertEqual("requirement_count_mismatch", server.client.messages[-1]["code"])
        self.assertIs(original, runtime.requirement_record)
        self.assertEqual("req-original", runtime.requirement_id)
        self.assertEqual(RuntimeState.RUNNING, runtime.state)
        self.assertEqual([], runtime.events)

    async def test_new_requirement_without_model_does_not_reapply_old_model(self):
        runtime, server = self.make_server()
        runtime.configure_artificial_society({"model_id": "model-old", "requirement_id": "req-original"})
        runtime.events.clear()
        await self.reset(server)
        self.assertEqual("init", server.client.messages[-1]["type"], server.client.messages)
        self.assertEqual("req-new", runtime.requirement_id)
        self.assertIsNone(runtime.artificial_society_record)
        self.assertIsNone(runtime.artificial_society_id)

    async def test_same_requirement_reset_keeps_existing_model(self):
        runtime, server = self.make_server()
        model = {"model_id": "model-old", "requirement_id": "req-original"}
        runtime.configure_artificial_society(model)
        runtime.events.clear()
        await self.reset(server, requirement_id="req-original")
        self.assertEqual("init", server.client.messages[-1]["type"], server.client.messages)
        self.assertIs(model, runtime.artificial_society_record)
        self.assertEqual("close", runtime.events[0][0])

    async def test_configure_new_requirement_still_requires_explicit_reset(self):
        runtime, server = self.make_server()
        await server._handle_message(json.dumps({"action": "configure", "requirement_id": "req-new"}))
        self.assertEqual("requirement_requires_reset", server.client.messages[-1]["code"])
        self.assertEqual([], runtime.events)

    async def test_configure_new_model_on_retained_run_requires_explicit_reset(self):
        runtime, server = self.make_server()
        runtime.configure_artificial_society({
            "model_id": "model-old", "fingerprint": "fp-model-old",
            "requirement_id": "req-original",
        })
        runtime.events.clear()
        await server._handle_message(json.dumps({
            "action": "configure", "request_id": "model-change",
            "requirement_id": "req-original", "artificial_society_id": "model-new",
            "artificial_society_fingerprint": "fp-model-new",
        }))
        self.assertEqual("artificial_society_requires_reset", server.client.messages[-1]["code"])
        self.assertEqual("model-old", runtime.artificial_society_id)
        self.assertEqual(RuntimeState.PAUSED, runtime.state)
        self.assertEqual([], runtime.events)

    async def test_configure_and_reset_reject_wrong_model_fingerprint_without_stopping_runtime(self):
        for action in ("configure", "reset"):
            with self.subTest(action=action):
                runtime, server = self.make_server(RuntimeState.PAUSED)
                payload = {
                    "action": action,
                    "request_id": action,
                    "requirement_id": "req-new",
                    "artificial_society_id": "model-new",
                    "artificial_society_fingerprint": "wrong",
                }
                if action == "reset":
                    payload["run_id"] = runtime.run_id
                await server._handle_message(json.dumps(payload))
                expected_code = ("artificial_society_requires_reset" if action == "configure"
                                 else "artificial_society_version_mismatch")
                self.assertEqual(expected_code, server.client.messages[-1]["code"])
                self.assertEqual(RuntimeState.PAUSED, runtime.state)
                self.assertEqual(0, runtime.closed)
                self.assertEqual([], runtime.events)


@unittest.skipUnless(has_sumo(), "real SUMO binary is required")
class ResetBindingSumoTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec("shapely"), "observation metrics require shapely")
    async def test_real_sumo_reset_rebinds_requirement_with_or_without_new_model(self):
        await self._check_reset()

    async def test_real_sumo_reset_isolated_from_observation_geometry(self):
        # Exercise the real engine, repositories, reset and frames even when the
        # optional test environment has not installed the new geometry package.
        # The separate end-to-end test above retains the real collector.
        with patch("crowdsim.core.simulation_runtime.ObservationCollector.from_requirement", return_value=None):
            await self._check_reset()

    async def _check_reset(self):
        from crowdsim.domain.artificial_society_spec import ArtificialSocietySpec
        from crowdsim.domain.requirement_spec import RequirementSpec
        from crowdsim.infrastructure.artificial_society_repository import ArtificialSocietyRepository
        from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
        from crowdsim.infrastructure.requirement_repository import RequirementRepository
        from crowdsim_overlay_server import build_requirement_runtime, parse_args
        from tests.test_artificial_society_submission import artificial_society_payload, requirement_payload

        for include_new_model in (True, False):
            with self.subTest(include_new_model=include_new_model), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                requirements = RequirementRepository(root / "requirements")
                models = ArtificialSocietyRepository(root / "models", requirements=requirements)
                old_payload = requirement_payload()
                old_payload["population"]["total"] = 2
                old_requirement = requirements.create(RequirementSpec.parse(old_payload))
                new_payload = requirement_payload()
                new_payload["population"]["total"] = 3
                new_requirement = requirements.create(RequirementSpec.parse(new_payload))
                old_model = models.create(ArtificialSocietySpec.parse(artificial_society_payload(old_requirement)))
                new_model = models.create(ArtificialSocietySpec.parse(artificial_society_payload(new_requirement)))
                with patch.dict("os.environ", {"CROWDSIM_PERF": "0", "CROWDSIM_SYSTEM_PERF": "0"}), patch(
                    "crowdsim.core.simulation_runtime.PROJECT_ROOT", root
                ), patch(
                    "crowdsim.core.simulation_runtime.ExperimentRecorder",
                    side_effect=lambda *args, **kwargs: ExperimentRecorder(*args, **kwargs, root=root / "runs"),
                ):
                    runtime = build_requirement_runtime(parse_args([]), old_requirement)
                    server = OverlayServer(runtime, "unused", 0, requirement_repository=requirements,
                                           artificial_society_repository=models)
                    server.client = Client()
                    try:
                        await server._handle_message(json.dumps({
                            "action": "configure", "request_id": "initial",
                            "requirement_id": old_requirement["requirement_id"],
                            "artificial_society_id": old_model["model_id"],
                        }))
                        self.assertFalse([row for row in server.client.messages if row["type"] == "error"],
                                         server.client.messages)
                        self.assertEqual(RuntimeState.READY, runtime.state)
                        runtime.start()
                        await runtime.tick_async()
                        runtime.pause()
                        old_run, old_writer = runtime.run_id, runtime.recorder.decision_writer
                        request = {
                            "action": "reset", "request_id": "replace", "run_id": old_run,
                            "requirement_id": new_requirement["requirement_id"], "seed": 17,
                        }
                        if include_new_model:
                            request["artificial_society_id"] = new_model["model_id"]
                        server.client.messages.clear()
                        await server._handle_message(json.dumps(request))
                        self.assertFalse([row for row in server.client.messages if row["type"] == "error"],
                                         server.client.messages)
                        init = next(row for row in server.client.messages if row["type"] == "init")
                        self.assertEqual("READY", init["runtime_state"])
                        self.assertEqual("replace", init["request_id"])
                        self.assertFalse(init["resumed"])
                        self.assertNotEqual(old_run, runtime.run_id)
                        self.assertTrue(old_writer.closed)
                        self.assertEqual(new_requirement["requirement_id"], runtime.requirement_id)
                        self.assertEqual(3, init["demand"]["planned"])
                        self.assertEqual(17, runtime.random_seed)
                        self.assertEqual(new_model["model_id"] if include_new_model else None,
                                         runtime.artificial_society_id)
                        runtime.start()
                        await runtime.tick_async()
                        self.assertEqual(RuntimeState.RUNNING, runtime.state)
                        self.assertEqual(3, runtime.frame()["metrics"]["population"]["planned"])
                    finally:
                        await server._stop_runtime()


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
                  "artificial_society_id": "", "artificial_society_fingerprint": "",
                  "location_id": self.runtime.location_id, "network_sha256": self.runtime.network_sha256}
        config.update(fields)
        await self.command("attach_run", request_id=f"attach-{id(config)}-{len(self.client.messages)}", **config)

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

    async def test_attach_rejects_other_artificial_society_version(self):
        self.runtime.artificial_society_record = {
            "model_id": "model-one", "fingerprint": "fingerprint-one",
        }
        self.runtime.artificial_society_id = "model-one"
        await self.attach(artificial_society_id="model-one",
                          artificial_society_fingerprint="fingerprint-one")
        self.assertEqual("init", self.client.messages[0]["type"])
        self.client.messages.clear()
        for fields in (
            {"artificial_society_id": "model-two", "artificial_society_fingerprint": "fingerprint-two"},
            {"artificial_society_id": "model-one", "artificial_society_fingerprint": "wrong"},
            {"artificial_society_id": "", "artificial_society_fingerprint": ""},
        ):
            await self.attach(**fields)
            self.assertEqual("run_artificial_society_mismatch", self.client.messages[-1]["code"])
        self.assertEqual(0, self.runtime.resets)

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
