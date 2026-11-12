"""Service bundle for the standalone CrowdSim experiment API."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .controller_registry import ControllerRegistry
from .gateway import CrowdSimGateway
from .gateway_events import EventBus
from .orchestrator import ExperimentOrchestrator
from .report_service import ReportService
from .repository import CrowdSimRepository
from .routing_service import RoutingService
from .during_strategy import DuringStrategyService
from .class2_comparison import Class2ComparisonService


class ControlServiceBundle:
    def __init__(self, *, gateway_url: str | None = None, data_dir: str | None = None, llm_client=None) -> None:
        default_data_dir = Path(__file__).resolve().parents[2] / "runs" / "control_experiments"
        storage = data_dir or os.environ.get("CROWDSIM_EXPERIMENT_DIR") or str(default_data_dir)
        self.repository = CrowdSimRepository(data_dir=storage)
        self.repository.recover_during_observations()
        self.gateway = CrowdSimGateway(gateway_url or os.environ.get("CROWDSIM_WS_URL", "ws://127.0.0.1:8765"))
        self.registry = ControllerRegistry(llm_client=llm_client)
        self.events = EventBus()
        self.orchestrator = ExperimentOrchestrator(self.repository, self.gateway, self.registry, event_bus=self.events)
        self.reports = ReportService(self.repository)
        from .during_observation_images import DuringObservationImages
        self.observation_images = DuringObservationImages(self.repository)
        self.during_strategies = DuringStrategyService(self.repository, self.reports)
        self.routing = RoutingService(self.gateway, llm_client=llm_client)
        from dialog_storage.repository import ConversationRepository
        from dialog_storage.during_requirements import DuringRequirementRepository
        from dialog_storage.during_artifacts import DuringArtifactRepository
        requirements = DuringRequirementRepository(ConversationRepository().root / 'during-requirements')
        artifacts = DuringArtifactRepository(ConversationRepository().root / 'during-artifacts', requirements)
        self.class2_comparisons = Class2ComparisonService(Path(storage) / 'class2_comparisons', self.repository,
                                                        self.gateway, requirements, artifacts, self.prepare_class2_requirement)

    def prepare_class2_requirement(self, request):
        parsed = urlsplit(self.gateway.url)
        if parsed.scheme not in {'ws', 'wss'} or not parsed.hostname:
            raise RuntimeError('CROWDSIM_UNAVAILABLE')
        url = urlunsplit(('https' if parsed.scheme == 'wss' else 'http', parsed.netloc,
                         '/api/v1/during/class2/requirements', '', ''))
        response = httpx.post(url, json=request, timeout=120,
                              trust_env=parsed.hostname not in {'localhost', '127.0.0.1', '::1'})
        result = response.json()
        if not response.is_success or result.get('type') != 'requirement_accepted':
            raise RuntimeError(result.get('message') or '第二类实验需求未被后端确认')
        return result

    def submit_requirement(self, requirement: dict, *, timeout: float = 20.0) -> dict:
        """Save through the simulator's HTTP API without acquiring its WebSocket."""
        parsed = urlsplit(self.gateway.url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise RuntimeError("CROWDSIM_UNAVAILABLE")
        url = urlunsplit(("https" if parsed.scheme == "wss" else "http", parsed.netloc,
                          "/api/v1/requirements", "", ""))
        try:
            response = httpx.post(url, json={"requirement": requirement}, timeout=timeout,
                                  trust_env=parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
        except httpx.TimeoutException as exc:
            raise TimeoutError("CROWDSIM_REQUIREMENT_TIMEOUT") from exc
        except httpx.RequestError as exc:
            raise RuntimeError("CROWDSIM_UNAVAILABLE") from exc
        if response.status_code == 404:
            raise RuntimeError("CROWDSIM_REQUIREMENT_ENDPOINT_UNAVAILABLE")
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("CROWDSIM_REQUIREMENT_INVALID_RESPONSE") from exc
        if not isinstance(result, dict):
            raise RuntimeError("CROWDSIM_REQUIREMENT_INVALID_RESPONSE")
        if result.get("type") == "error":
            code = str(result.get("code") or "INVALID_REQUIREMENT")
            message = str(result.get("message") or "requirement submission failed")
            raise ValueError(f"{code}: {message}")
        if not response.is_success or result.get("type") != "requirement_accepted":
            raise RuntimeError("CROWDSIM_REQUIREMENT_INVALID_RESPONSE")
        return result
