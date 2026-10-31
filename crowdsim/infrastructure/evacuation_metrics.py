"""Observe journey completion and density changes; never alter pedestrian routes.

All timestamps are SUMO seconds. Normal arrivals are a deliberately simplified
completion criterion, not evidence of crossing a geographical safety boundary.
"""

from copy import deepcopy

from crowdsim.domain.observation_config import EVACUATION_METRIC_IDS


class EvacuationTracker:
    EVACUATION_POLICIES = {"police_guidance", "temporary_diversion"}

    def __init__(self, metric_ids):
        self.metric_ids = tuple(name for name in EVACUATION_METRIC_IDS if name in metric_ids)
        self.initial = None
        self.strategy = None
        self.final = None
        self.latest = None
        self.target_ids = None
        self.progress = {}
        self.policy_applications = []

    @staticmethod
    def density_snapshot(sample):
        valid = sample["global"]["invalid_position_count"] == 0
        return {
            "time_seconds": sample["time_seconds"], "snapshot_id": sample["snapshot_id"],
            "valid": valid, "person_count": sample["global"]["person_count"],
            "area_m2": sample["global"]["area_m2"],
            "density_person_per_m2": sample["global"]["density_person_per_m2"] if valid else None,
            "cells": {row["cell_id"]: {
                "area_m2": row["area_m2"], "person_count": row["person_count"],
                "density_person_per_m2": row["density_person_per_m2"] if valid else None,
            } for row in sample["cells"]},
        }

    def start_event(self, now, ledger):
        if self.initial is not None:
            return False
        if self.latest is None or self.latest["time_seconds"] != now:
            raise ValueError("event start requires the current observation snapshot")
        self.initial = deepcopy(self.latest)
        self.target_ids = set(ledger.planned_ids) | set(ledger.departed_ids)
        self._progress(ledger)
        return True

    def policy_applied(self, record, data):
        # Called only after the intervention executor actually succeeds. The
        # first effective intervention establishes the baseline once per run.
        effective = record["name"] in self.EVACUATION_POLICIES
        starts = effective and self.strategy is None and self.final is None
        application = {**record, "request_id": data.get("request_id"),
                       "strategy_id": data.get("strategy_id") or data.get("id"),
                       "starts_evacuation": starts}
        self.policy_applications.append(application)
        if starts:
            if self.latest is None or self.latest["time_seconds"] != record["applied_at"]:
                raise ValueError("strategy application requires the current observation snapshot")
            self.strategy = deepcopy(self.latest)

    def _progress(self, ledger):
        if self.target_ids is None or ledger is None:
            return
        target = self.target_ids
        unexpected = (set(ledger.planned_ids) | set(ledger.departed_ids)) - target
        removed = set(ledger.explicitly_removed)
        unknown = set(ledger.unknown_disappearances)
        self.progress = {
            "target_person_count": len(target),
            "active_person_count": len(ledger.active_ids),
            "pending_person_count": len(target - ledger.departed_ids - ledger.arrived_ids),
            "normally_arrived_person_count": len(target & ledger.arrived_ids),
            "remaining_person_count": len(target - ledger.arrived_ids),
            "explicitly_removed_person_count": len(removed),
            "unknown_disappearance_count": len(unknown),
            "unexpected_person_count": len(unexpected),
            "conservation_error": ledger.conservation_error,
        }

    def observe(self, sample, ledger=None):
        self.latest = self.density_snapshot(sample)
        self._progress(ledger)
        p = self.progress
        if ledger is not None and self.initial is not None and self.final is None and p.get("target_person_count", 0) > 0 and (
            p["remaining_person_count"] == p["active_person_count"] == p["pending_person_count"] == 0
            and p["explicitly_removed_person_count"] == p["unknown_disappearance_count"] == 0
            and p["unexpected_person_count"] == p["conservation_error"] == 0
        ):
            self.final = deepcopy(self.latest)
        if "absolute-evacuation-density" in self.metric_ids:
            for row in sample["cells"]:
                initial = self.initial["cells"][row["cell_id"]]["density_person_per_m2"] if self.initial else None
                current = row["density_person_per_m2"] if self.latest["valid"] else None
                row["density_valid"] = self.latest["valid"]
                row["absolute_initial_difference_person_per_m2"] = (
                    abs(initial - current) if initial is not None and current is not None else None
                )
        sample["evacuation"] = self.summary()
        valid = sample["valid_metric_ids"]
        if self.initial and self.latest["valid"] and "absolute-evacuation-density" in self.metric_ids:
            if self.initial["valid"]:
                valid.append("absolute-evacuation-density")
        if self.final:
            valid.extend(name for name in ("evacuation-time", "evacuation-efficiency")
                         if name in self.metric_ids and sample["evacuation"]["metrics"][name]["status"] == "complete")

    def summary(self, run_status="running", reason=None):
        t0 = self.initial["time_seconds"] if self.initial else None
        ta = self.strategy["time_seconds"] if self.strategy else None
        te = self.final["time_seconds"] if self.final else None
        now = self.latest["time_seconds"] if self.latest else None
        if t0 is None:
            status = "not_started"
        elif not self.progress.get("target_person_count"):
            status = "no_target_population"
        elif any(self.progress.get(key, 0) for key in (
            "explicitly_removed_person_count", "unknown_disappearance_count", "unexpected_person_count", "conservation_error"
        )):
            status = "invalid_population_accounting"
        elif te is not None:
            status = "complete"
        else:
            status = "incomplete" if run_status == "complete" else run_status if run_status in {"error", "interrupted"} else "in_progress"
        duration = te - ta if te is not None and ta is not None and t0 is not None else None
        start_density = self.strategy["density_person_per_m2"] if self.strategy else None
        end_density = self.final["density_person_per_m2"] if self.final else None
        metrics = {}
        for name in self.metric_ids:
            metric_status = status
            value = None
            unit = "person/m2" if name == "absolute-evacuation-density" else "simulation_s"
            if name in {"evacuation-time", "evacuation-efficiency"}:
                if ta is None:
                    metric_status = "not_started"
                elif status == "complete":
                    value = duration
                    if name == "evacuation-efficiency":
                        if start_density is None or end_density is None:
                            metric_status, value = "invalid_density_baseline", None
                        elif duration <= 0:
                            metric_status, value = "zero_duration", None
                        else:
                            value = (start_density - end_density) / duration
                if name == "evacuation-efficiency":
                    unit = "person/(m2*simulation_s)"
            elif self.initial and not self.initial["valid"]:
                metric_status = "invalid_density_baseline"
            metrics[name] = {"status": metric_status, "value": value, "unit": unit}
            if name == "absolute-evacuation-density":
                metrics[name]["representation"] = "per_uniform_cell_time_series"
        return {
            "basis": "all_run_pedestrians_normally_arrived_in_sumo",
            "status": status, "end_reason": reason,
            "event_start_time_seconds": t0, "strategy_applied_time_seconds": ta,
            "completion_time_seconds": te, "last_time_seconds": now,
            "total_event_time_seconds": te - t0 if te is not None else None,
            "elapsed_since_strategy_seconds": max(0, (te if te is not None else now) - ta) if ta is not None and now is not None else None,
            "density_at_strategy_person_per_m2": start_density,
            "density_at_completion_person_per_m2": end_density,
            "first_evacuation_policy": next((item for item in self.policy_applications if item["starts_evacuation"]), None),
            "policy_application_count": len(self.policy_applications),
            "progress": dict(self.progress), "metrics": metrics,
        }

    def evidence(self, run_status="running", reason=None):
        return {**self.summary(run_status, reason),
                "target_person_ids": sorted(self.target_ids) if self.target_ids is not None else None,
                "initial_density": self.initial, "strategy_density": self.strategy, "final_density": self.final,
                "policy_applications": list(self.policy_applications)}
