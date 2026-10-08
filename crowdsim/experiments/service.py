"""Service bundle for the standalone CrowdSim experiment API."""

from __future__ import annotations

import os
from pathlib import Path

from .controller_registry import ControllerRegistry
from .gateway import CrowdSimGateway
from .gateway_events import EventBus
from .orchestrator import ExperimentOrchestrator
from .report_service import ReportService
from .repository import CrowdSimRepository
from .routing_service import RoutingService


class ControlServiceBundle:
    def __init__(self, *, gateway_url: str | None = None, data_dir: str | None = None, llm_client=None) -> None:
        default_data_dir = Path(__file__).resolve().parents[2] / "runs" / "control_experiments"
        storage = data_dir or os.environ.get("CROWDSIM_EXPERIMENT_DIR") or str(default_data_dir)
        self.repository = CrowdSimRepository(data_dir=storage)
        self.gateway = CrowdSimGateway(gateway_url or os.environ.get("CROWDSIM_WS_URL", "ws://127.0.0.1:8765"))
        self.registry = ControllerRegistry(llm_client=llm_client)
        self.events = EventBus()
        self.orchestrator = ExperimentOrchestrator(self.repository, self.gateway, self.registry, event_bus=self.events)
        self.reports = ReportService(self.repository)
        self.routing = RoutingService(self.gateway, llm_client=llm_client)

    def submit_requirement(self, requirement: dict, *, timeout: float = 20.0) -> dict:
        """Submit a requirement through the gateway's single CrowdSim connection."""
        try:
            request_id = self.gateway.submit({
                "action": "submit_requirement",
                "requirement": requirement,
            })
        except RuntimeError as exc:
            raise RuntimeError("CROWDSIM_UNAVAILABLE") from exc
        result = self.gateway.wait_for_ack(request_id, timeout=timeout)
        if result is None:
            self.gateway.forget_request(request_id)
            raise TimeoutError("CROWDSIM_REQUIREMENT_TIMEOUT")
        if result.get("type") == "error":
            code = str(result.get("code") or "INVALID_REQUIREMENT")
            message = str(result.get("message") or "requirement submission failed")
            raise ValueError(f"{code}: {message}")
        if result.get("type") != "requirement_accepted":
            raise RuntimeError("CROWDSIM_REQUIREMENT_INVALID_RESPONSE")
        return result
