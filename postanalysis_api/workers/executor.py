"""Executor protocol and explicit unavailable implementation for injection.

The production post app uses workers.sumo_batch.SumoBatchExecutor, separate
from the C0-C5 during service. Neither implementation returns preview numbers.
"""

from __future__ import annotations

from typing import Protocol

from postanalysis_api.schemas.common import ApiError


class SimulationExecutor(Protocol):
    def capabilities(self) -> dict: ...
    def submit(self, task: dict, on_event) -> None: ...
    def cancel(self, task_id: str) -> None: ...


class UnavailableExecutor:
    def capabilities(self) -> dict:
        return {"ready": False, "reason": "SUMO_BATCH_ADAPTER_MISSING", "scenarioIds": [],
                "modelIds": [], "modelVersions": {}, "geometryVersions": {}, "ruleVersions": [],
                "actionTypes": [], "actionSchemas": {}, "regionMap": {}, "metricIds": [],
                "maxParallelism": 0, "maxTotalRuns": 0}

    def submit(self, task: dict, on_event) -> None:
        raise ApiError("CAPABILITY_UNAVAILABLE", "事后 SUMO 批量执行器尚未接入", 503)

    def cancel(self, task_id: str) -> None:
        raise ApiError("CAPABILITY_UNAVAILABLE", "事后 SUMO 批量执行器尚未接入", 503)
