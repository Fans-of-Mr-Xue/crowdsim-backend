"""Merge regressions using in-memory commands; no listeners or SUMO processes."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from crowdsim.core.simulation_runtime import RuntimeState, SimulationRuntime
from crowdsim.domain.requirement_spec import (
    RequirementSpec, RequirementValidationError, requirement_runtime_summary,
)
from crowdsim.experiments.contracts import ContractError, validate_experiment_config
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.websocket_server import OverlayServer
from crowdsim.scenarios.generated_network_demand import NetworkDemandSpec
from crowdsim_overlay_server import SCENARIO_PRESETS
from tests.test_requirement_submission import requirement_payload
from tests.test_retained_simulation_session import Client, Runtime


class BoundRuntime(Runtime):
    def __init__(self, record):
        super().__init__()
        self.configure_requirement(record)
        self.demand_count = record["requirement"]["population"]["total"]
        self.seed = None

    def configure_requirement(self, record):
        super().configure_requirement(record)
        self.location_id = record["requirement"]["spatial_scope"]["location_id"]

    def supports_requirement(self, record):
        return self.location_id == record["requirement"]["spatial_scope"]["location_id"]

    def reset(self, count=None, *, seed=None, **kwargs):
        super().reset(count, **kwargs)
        if count is not None:
            self.demand_count = count
        self.seed = seed

    def diagnostics(self):
        return {"requirement": requirement_runtime_summary(self.requirement_record)}

    def init_frame(self):
        return {**super().init_frame(),
                "requirement": requirement_runtime_summary(self.requirement_record)}


class MemoryGateway:
    def __init__(self, server):
        self.server = server
        self.submitted = []
        self.replies = {}

    def add_listener(self, callback):
        pass

    def submit(self, payload):
        request_id = f"request-{len(self.submitted)}"
        self.submitted.append(deepcopy(payload))
        before = len(self.server.client.messages)
        asyncio.run(self.server._handle_message(json.dumps({**payload, "request_id": request_id})))
        replies = [message for message in self.server.client.messages[before:]
                   if message.get("request_id") == request_id]
        self.replies[request_id] = replies[-1] if replies else None
        return request_id

    def wait_for_ack(self, request_id, timeout):
        return self.replies.pop(request_id)


@pytest.fixture
def bound_server(tmp_path):
    repository = RequirementRepository(tmp_path)
    payload = requirement_payload()
    payload["population"]["total"] = 100
    record = repository.create(RequirementSpec.parse(payload))
    runtime = BoundRuntime(record)
    server = OverlayServer(runtime, "unused", 0, requirement_repository=repository)
    server.client = Client()
    return server, repository, record


@pytest.mark.parametrize("count", [700, 0])
def test_experiment_population_uses_new_immutable_requirement(bound_server, count):
    server, repository, original = bound_server
    source_path = repository.root / f'{original["requirement_id"]}.json'
    source_bytes = source_path.read_bytes()
    gateway = MemoryGateway(server)
    orchestrator = ExperimentOrchestrator(Mock(), gateway, Mock())

    reset = orchestrator._prepare_reset_payload({"population": count}, 37)
    assert reset["requirement_id"] != original["requirement_id"]
    derived = repository.load(reset["requirement_id"])
    assert derived["purpose"] == "experiment"
    assert derived["requirement"]["population"]["total"] == count
    expected = deepcopy(original["requirement"])
    expected["population"]["total"] = count
    assert derived["requirement"] == expected

    reply = gateway.wait_for_ack(gateway.submit(reset), timeout=1)
    assert reply["type"] == "init"
    assert server.simulator.demand_count == count
    assert server.simulator.seed == 37
    assert server.simulator.requirement_id == derived["requirement_id"]
    assert source_path.read_bytes() == source_bytes


def test_ordinary_reset_still_rejects_mismatched_population(bound_server):
    server, _, _ = bound_server
    gateway = MemoryGateway(server)
    reply = gateway.wait_for_ack(gateway.submit({"action": "reset", "count": 700, "seed": 37}), timeout=1)
    assert reply["code"] == "requirement_count_mismatch"
    assert server.simulator.closed == 0
    assert server.simulator.resets == 0


def test_same_population_reuses_current_requirement(bound_server):
    server, repository, _ = bound_server
    gateway = MemoryGateway(server)
    orchestrator = ExperimentOrchestrator(Mock(), gateway, Mock())
    reset = orchestrator._prepare_reset_payload({"population": 100}, 37)
    assert reset == {"action": "reset", "count": 100, "seed": 37}
    assert gateway.submitted == [{"action": "get_status"}]
    assert len(list(repository.root.glob("*.json"))) == 1


def test_unspecified_population_keeps_existing_reset_protocol(bound_server):
    gateway = MemoryGateway(bound_server[0])
    orchestrator = ExperimentOrchestrator(Mock(), gateway, Mock())
    assert orchestrator._prepare_reset_payload({}, 37) == {"action": "reset", "seed": 37}
    assert gateway.submitted == []


def test_requirement_lookup_failure_cannot_reset_existing_run(bound_server):
    gateway = MemoryGateway(bound_server[0])
    gateway.wait_for_ack = Mock(return_value={"type": "error", "code": "lookup_failed"})
    orchestrator = ExperimentOrchestrator(Mock(), gateway, Mock())
    with pytest.raises(RuntimeError, match="requirement lookup failed"):
        orchestrator._prepare_reset_payload({"population": 700}, 37)
    assert gateway.submitted == [{"action": "get_status"}]
    assert bound_server[0].simulator.closed == 0


def test_scene_replacement_forwards_seed_to_new_runtime(bound_server):
    server, repository, _ = bound_server
    old_runtime = server.simulator
    payload = requirement_payload("east-nanjing-road")
    payload["population"]["total"] = 700
    new_record = repository.create(RequirementSpec.parse(payload))

    def factory(record):
        runtime = BoundRuntime(record)
        runtime.state = RuntimeState.CREATED
        return runtime

    server.runtime_factory = factory
    gateway = MemoryGateway(server)
    reply = gateway.wait_for_ack(gateway.submit({
        "action": "reset", "requirement_id": new_record["requirement_id"], "seed": 91,
    }), timeout=1)
    assert reply["type"] == "init"
    assert server.simulator is not old_runtime
    assert old_runtime.closed == 1
    assert server.simulator.seed == 91
    assert server.simulator.demand_count == 700
    assert server.simulator.location_id == "east-nanjing-road"


@pytest.mark.parametrize("seed", [True, 1.5, "91"])
def test_invalid_seed_is_rejected_before_replacing_run(bound_server, seed):
    server, _, record = bound_server
    gateway = MemoryGateway(server)
    reply = gateway.wait_for_ack(gateway.submit({
        "action": "reset", "requirement_id": record["requirement_id"], "seed": seed,
    }), timeout=1)
    assert reply["code"] == "invalid_seed"
    assert server.simulator.closed == 0


def test_empty_population_is_experimental_only(bound_server):
    server, repository, _ = bound_server
    payload = requirement_payload()
    payload["population"]["total"] = 0
    with pytest.raises(RequirementValidationError):
        RequirementSpec.parse(payload)
    spec = RequirementSpec.parse(payload, allow_empty_population=True)
    with pytest.raises(RequirementValidationError):
        repository.create(spec)
    gateway = MemoryGateway(server)
    reply = gateway.wait_for_ack(gateway.submit({
        "action": "submit_requirement", "requirement": payload,
    }), timeout=1)
    assert reply["code"] == "invalid_requirement"


@pytest.mark.parametrize("population", [True, -1, 10001, 1.5, "700"])
def test_experiment_population_validation(population):
    with pytest.raises(ContractError, match="scenario.population"):
        validate_experiment_config({
            "name": "population validation", "scenario": {"population": population, "seedSet": [1]},
            "methods": ["C0"],
        })


@pytest.mark.parametrize("mode", ["generated_hotspot", "generated_network"])
def test_seed_updates_demand_and_profiles_on_init_and_reset(tmp_path, mode):
    config = tmp_path / "unused.sumocfg"
    config.write_text('<configuration><input/></configuration>')
    spec = (replace(SCENARIO_PRESETS["hotspot"].hotspot_demand_spec, seed=7)
            if mode == "generated_hotspot" else NetworkDemandSpec(seed=7))
    keyword = "hotspot_demand_spec" if mode == "generated_hotspot" else "network_demand_spec"
    with patch("crowdsim.infrastructure.sumo_adapter.discover_sumo_binary", return_value="never-executed-sumo"):
        runtime = SimulationRuntime(
            config, demand_mode=mode, random_seed=77, location_id="east-nanjing-road",
            road_network_url="/network.json", **{keyword: spec},
        )
        try:
            assert runtime.generated_demand_spec.seed == 77
            assert runtime.population.profile_sampler.seed == 77
            assert spec.seed == 7
            with patch.object(SimulationRuntime, "initialize", return_value=None):
                runtime.reset(12, seed=99)
            assert runtime.generated_demand_spec.seed == 99
            assert runtime.population.profile_sampler.seed == 99
            assert runtime.demand_count == 12
            assert runtime.location_id == "east-nanjing-road"
            assert runtime.road_network_url == "/network.json"
        finally:
            runtime.close()
