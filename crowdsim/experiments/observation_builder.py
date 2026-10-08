"""Build stable controller observations from CrowdSim frames."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from typing import Any, Mapping

from .contracts import SCHEMA_VERSION, finite, public_time, validate_observation


class ObservationBuilder:
    def __init__(self, history_size: int = 20) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)

    @staticmethod
    def _risk(global_metrics: dict[str, Any]) -> float:
        explicit = finite(global_metrics.get("riskIndex"))
        if explicit is not None:
            return max(0.0, min(1.0, explicit))
        if int(global_metrics.get("activePopulation", global_metrics.get("pedestrian_count", 0)) or 0) <= 0:
            return 0.0
        congestion = finite(global_metrics.get("congestion"), 0.0) or 0.0
        density_levels = global_metrics.get("density_levels") or {}
        high = float(density_levels.get("critical", 0) or density_levels.get("high", 0) or 0)
        population = max(1.0, float(global_metrics.get("pedestrian_count", 0) or 0))
        speed = finite(global_metrics.get("pedestrian_risk_speed"), finite(global_metrics.get("pedestrian_avg_speed"), 1.4)) or 0.0
        return max(0.0, min(1.0, 0.45 * congestion + 0.30 * min(1.0, high / population * 5) + 0.25 * (1 - min(speed / 1.4, 1))))

    @staticmethod
    def _regions(metrics: dict[str, Any]) -> list[dict[str, Any]]:
        hotspots = metrics.get("hotspot_metrics") or {}
        if isinstance(hotspots, list):
            return [deepcopy(item) for item in hotspots]
        result = []
        if isinstance(hotspots, Mapping):
            for region_id, values in hotspots.items():
                item = dict(values or {}) if isinstance(values, Mapping) else {"value": values}
                item.setdefault("regionId", str(region_id))
                population = int(item.get("person_count", 0) or 0)
                density = finite(item.get("density", item.get("density_person_per_m2")), 0.0) or 0.0
                speed = finite(item.get("speed", item.get("avg_speed_mps", item.get("avg_walking_speed_mps"))), 0.0) or 0.0
                slow = int(item.get("low_speed_count", item.get("slow_walking_count", 0)) or 0)
                item.setdefault("density", density)
                item.setdefault("speed", speed)
                item.setdefault("riskIndex", 0.0 if population <= 0 else max(0.0, min(
                    1.0,
                    0.5 * min(density / 4.0, 1.0)
                    + 0.3 * (1.0 - min(speed / 1.4, 1.0))
                    + 0.2 * min(slow / max(1, population), 1.0),
                )))
                entries = item.get("entries") or {}
                entry_edges = entries.get("edges") if isinstance(entries, Mapping) else None
                if entry_edges:
                    item.setdefault("entryId", str(entry_edges[0]))
                core = item.get("core") or {}
                target_edges = core.get("target_edges") if isinstance(core, Mapping) else None
                if target_edges:
                    item.setdefault("edgeId", str(target_edges[0]))
                result.append(item)
        return result

    @staticmethod
    def _edges(metrics: dict[str, Any]) -> list[dict[str, Any]]:
        edges = metrics.get("edge_metrics") or {}
        if isinstance(edges, list):
            return [deepcopy(item) for item in edges]
        if isinstance(edges, Mapping):
            result = []
            for edge_id, values in edges.items():
                item = dict(values) if isinstance(values, Mapping) else {"value": values}
                item.setdefault("density", finite(item.get("density_person_per_m2")))
                item.setdefault("speed", finite(item.get("avg_speed_mps"), 0.0))
                result.append({"edgeId": str(edge_id), **item})
            return result
        return []

    def build(self, frame: Mapping[str, Any], *, run_id: str, last_decision=None, last_effect=None) -> dict[str, Any]:
        metrics = dict(frame.get("metrics") or {})
        population = metrics.get("population") or {}
        active = int(metrics.get("pedestrian_count", 0) or 0)
        arrived = int(population.get("arrived", population.get("arrived_count", 0)) or 0) if isinstance(population, Mapping) else 0
        planned = int(population.get("planned", population.get("planned_count", 0)) or 0) if isinstance(population, Mapping) else 0
        total = max(planned, active + arrived, 1)
        edge_values = list((metrics.get("edge_metrics") or {}).values())
        densities = [
            finite(item.get("density_person_per_m2"))
            for item in edge_values if isinstance(item, Mapping)
        ]
        densities = [value for value in densities if value is not None]
        global_metrics = {
            "activePopulation": active,
            "evacuatedPopulation": arrived,
            "meanDensity": finite(metrics.get("mean_density"), sum(densities) / len(densities) if densities else 0.0),
            "maxDensity": finite(metrics.get("max_density"), max(densities) if densities else 0.0),
            "meanSpeed": finite(metrics.get("pedestrian_avg_speed"), 0.0),
            "completionRate": arrived / total,
            "efficiency": max(0.0, min(1.0, arrived / total + 0.25 * min((finite(metrics.get("pedestrian_avg_speed"), 0.0) or 0.0) / 1.4, 1))),
            "congestion": finite(metrics.get("congestion"), 0.0),
        }
        global_metrics["riskIndex"] = self._risk({**metrics, **global_metrics})
        sim_time = finite(frame.get("step_seconds", frame.get("step")), 0.0) or 0.0
        previous = self._history[-1] if self._history else None
        previous_risk = ((previous or {}).get("global") or {}).get("riskIndex")
        trend = {"risk": 0.0 if previous_risk is None else global_metrics["riskIndex"] - float(previous_risk)}
        observation = validate_observation({
            "schemaVersion": SCHEMA_VERSION,
            "observationId": str(frame.get("snapshot_id") or f"{run_id}:{int(frame.get('step_index', 0))}"),
            "runId": run_id,
            "snapshotId": frame.get("snapshot_id"),
            "tick": int(frame.get("step_index", 0) or 0),
            "simTimeSeconds": sim_time,
            "receivedAt": public_time(),
            "global": global_metrics,
            "regions": self._regions(metrics),
            "edges": self._edges(metrics),
            "hazards": list(frame.get("flood_points") or []) + list(frame.get("events") or []),
            "strategyStats": {},
            "trend": trend,
            "lastDecision": deepcopy(last_decision),
            "lastEffect": deepcopy(last_effect),
            "historyWindow": [{"observationId": item["observationId"], "simTimeSeconds": item["simTimeSeconds"], "global": item["global"]} for item in self._history],
            "availability": {
                "regions": "available" if metrics.get("hotspot_metrics") is not None else "unavailable",
                "edges": "available" if metrics.get("edge_metrics") is not None else "unavailable",
                "strategyStats": "unavailable",
            },
        })
        self._history.append(deepcopy(observation))
        return observation
