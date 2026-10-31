"""Per-step backend evidence for a bound during preset."""
from collections import Counter
from crowdsim.domain.during_crowding import CrowdingTracker
from crowdsim.domain.during_preset import preset_fingerprint


class DuringObservation:
    def __init__(self, preset):
        self.preset = preset
        self.fingerprint = preset_fingerprint(preset)
        self.tracker = CrowdingTracker(preset["phenomenon"])
        self.t95 = None
        self.peaks = {"core_count": 0, "core_density_person_per_m2": 0.0,
                      "blocked_low_speed_count": 0, "control_held_count": 0}

    def measure(self, runtime, *, finished=False):
        edges = self.preset["phenomenon"]["scope_edges"]
        people = [item for item in runtime.current.persons.values() if item.edge_id in edges]
        indicators = [runtime.visual_states.get(item.person_id, {}) for item in people]
        counts = Counter(item.get("visual_state") for item in indicators if item.get("density_valid"))
        population = runtime.population.diagnostics()
        sample = {
            "time_seconds": runtime.time_seconds, "core_count": len(people),
            "crowded_count": counts["crowded"], "high_risk_count": counts["high_risk"],
            "unknown_count": sum(not item.get("density_valid", False) for item in indicators),
            "core_density_person_per_m2": runtime.latest_metrics["hotspots"][self.preset["hotspot_id"]]["density_person_per_m2"],
            "blocked_low_speed_count": runtime.latest_metrics.get("queue", {}).get("blocked_low_speed", 0),
            "control_held_count": runtime.latest_metrics.get("control_queue", {}).get("inflow_held", 0),
        }
        self.tracker.update(sample)
        for name in self.peaks:
            self.peaks[name] = max(self.peaks[name], sample[name])
        planned = population["planned"]
        completion = population["arrived"] / planned if planned else None
        if self.t95 is None and completion is not None and completion >= .95:
            self.t95 = runtime.time_seconds
        valid = (planned == self.preset["requirement"]["population"]["total"]
                 and population["conservation_error"] == 0 and not population["unknown_disappearances"]
                 and population["explicitly_removed"] == 0)
        all_arrived = population["arrived"] == planned and population["active"] == 0
        reached_end = runtime.time_seconds >= self.preset["experiment"]["duration_seconds"]
        end_reason = ("all_arrived" if all_arrived else "observation_horizon" if reached_end
                      else "premature_end") if finished else None
        return {
            "preset_id": self.preset["preset_id"], "preset_version": self.preset["version"],
            "preset_fingerprint": self.fingerprint, "time_seconds": runtime.time_seconds,
            "sample": sample, "phenomenon": self.tracker.snapshot(final=finished),
            "population": population, "population_valid": valid,
            "completion_rate": completion, "t95_from_start_seconds": self.t95,
            "peaks": dict(self.peaks), "finished": finished, "end_reason": end_reason,
            "run_complete": finished and valid and (all_arrived or reached_end),
        }
