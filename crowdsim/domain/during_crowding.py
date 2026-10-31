"""Incremental sustained-crowding definition, also usable for offline scoring."""
from copy import deepcopy
import math


class CrowdingTracker:
    """Consume simulation boundaries once, without retaining frame history."""

    def __init__(self, policy):
        self.policy = deepcopy(policy)
        self.minimum = policy["minimum_count"]
        self.fraction = policy["minimum_fraction"]
        self.confirmation = policy["confirmation_seconds"]
        self.max_gap = policy["maximum_sample_gap_seconds"]
        self.observation_start = policy.get("observation_start_seconds", 0.0)
        if (isinstance(self.observation_start, bool) or not isinstance(self.observation_start, (int, float))
                or not math.isfinite(self.observation_start) or self.observation_start < 0):
            raise ValueError("observation_start_seconds must be finite and non-negative")
        if type(self.minimum) is not int or self.minimum < 1:
            raise ValueError("minimum_count must be a positive integer")
        for name, value in (("minimum_fraction", self.fraction), ("confirmation_seconds", self.confirmation),
                            ("maximum_sample_gap_seconds", self.max_gap)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.fraction > 1:
            raise ValueError("minimum_fraction must not exceed one")
        self.episodes = []
        self.previous = self.last_input = None
        self.onset = self.confirmed_at = None
        self.sample_count = self.unavailable = self.gap_count = self.excluded_initialization = 0
        self.peak_count = self.peak_high_risk = 0
        self.peak_fraction = 0.0

    def _episode(self, end, reason, censored):
        return {"onset_seconds": self.onset, "confirmed_at_seconds": self.confirmed_at,
                "end_seconds": end, "duration_seconds": round(end - self.onset, 6),
                "end_reason": reason, "right_censored": censored}

    def _close(self, end, reason, censored):
        if self.confirmed_at is not None:
            self.episodes.append(self._episode(end, reason, censored))
        self.onset = self.confirmed_at = None

    def update(self, source):
        row = {key: source[key] for key in ("time_seconds", "core_count", "crowded_count", "high_risk_count", "unknown_count")}
        now = row["time_seconds"]
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise ValueError("time_seconds must be finite and non-negative")
        for key in ("core_count", "crowded_count", "high_risk_count", "unknown_count"):
            if type(row[key]) is not int or row[key] < 0:
                raise ValueError(f"{key} must be a non-negative integer")
        if row["crowded_count"] + row["high_risk_count"] + row["unknown_count"] > row["core_count"]:
            raise ValueError("visual-state counts exceed core population")
        if self.last_input is not None:
            delta = now - self.last_input["time_seconds"]
            if delta < 0:
                raise ValueError("samples must be ordered by simulation time")
            if delta == 0:
                if row != self.last_input:
                    raise ValueError("conflicting samples at the same simulation time")
                return
        self.last_input = row
        if now < self.observation_start:
            self.excluded_initialization += 1
            return
        if self.previous is None and now > self.observation_start + 1e-9:
            self.gap_count += 1
        if self.previous is not None and now - self.previous["time_seconds"] > self.max_gap + 1e-9:
            self.gap_count += 1
            self._close(self.previous["time_seconds"], "sampling_gap", True)
        self.sample_count += 1
        count = row["crowded_count"] + row["high_risk_count"]
        ratio = count / row["core_count"] if row["core_count"] else 0.0
        self.peak_count = max(self.peak_count, count)
        self.peak_high_risk = max(self.peak_high_risk, row["high_risk_count"])
        self.peak_fraction = max(self.peak_fraction, ratio)
        if row["unknown_count"]:
            self.unavailable += 1
            self._close(self.previous["time_seconds"] if self.previous else now, "density_unavailable", True)
        elif count >= self.minimum and ratio + 1e-12 >= self.fraction:
            if self.onset is None:
                self.onset = now
            if self.confirmed_at is None and now - self.onset + 1e-9 >= self.confirmation:
                self.confirmed_at = now
        else:
            self._close(now, "condition_cleared", False)
        self.previous = row

    def snapshot(self, *, final=False):
        episodes = deepcopy(self.episodes)
        if self.confirmed_at is not None:
            episodes.append(self._episode(self.previous["time_seconds"], "observation_end" if final else "ongoing", True))
        complete = self.sample_count > 0 and self.unavailable == 0 and self.gap_count == 0
        return {
            "definition_id": self.policy["id"], "definition_version": self.policy["version"],
            "occurred": True if episodes else False if complete else None,
            "coverage": "complete" if complete else "incomplete",
            "sample_count": self.sample_count, "unknown_sample_count": self.unavailable, "gap_count": self.gap_count,
            "observation_start_seconds": self.observation_start, "excluded_initialization_samples": self.excluded_initialization,
            "first_confirmed_at_seconds": episodes[0]["confirmed_at_seconds"] if episodes else None,
            "confirmed_duration_seconds": round(sum(item["duration_seconds"] for item in episodes), 6),
            "peak_crowded_count": self.peak_count, "peak_high_risk_count": self.peak_high_risk,
            "peak_crowded_fraction": self.peak_fraction, "episodes": episodes,
        }


def summarize_crowding(samples, policy):
    tracker = CrowdingTracker(policy)
    for sample in samples:
        tracker.update(sample)
    return tracker.snapshot(final=True)
