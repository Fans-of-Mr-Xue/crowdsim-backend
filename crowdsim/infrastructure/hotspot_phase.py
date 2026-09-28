"""Stateful, measurement-driven phase classification for finite crowd hotspots."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Mapping


PHASE_LABELS = {
    "normal": "正常流动",
    "building": "人群正在聚集",
    "congested": "拥堵已形成",
    "dispersing": "人群正在疏散",
    "residual_congestion": "外围仍有残余拥堵",
    "cleared": "聚集基本消散",
}


@dataclass(frozen=True)
class HotspotPhaseThresholds:
    building_person_count: int = 50
    building_core_density_person_per_m2: float = 0.15
    congested_core_density_person_per_m2: float = 1.5
    rising_rate_person_per_second: float = 0.1
    falling_rate_person_per_second: float = -0.1
    cleared_person_count: int = 10
    cleared_peak_fraction: float = 0.02
    cleared_core_density_person_per_m2: float = 0.05
    residual_backlog_person_count: int = 20
    cleared_remaining_visitor_count: int = 10
    cleared_blocked_person_count: int = 5
    trend_window_seconds: float = 20.0
    confirmation_seconds: float = 10.0

    @classmethod
    def from_mapping(cls, raw: Mapping | None) -> "HotspotPhaseThresholds":
        if not raw:
            return cls()
        values = {field: raw[field] for field in cls.__dataclass_fields__ if field in raw}
        return cls(**values)


class HotspotPhaseTracker:
    """Classify a hotspot without using scripted timestamps or synthetic counts."""

    def __init__(self, thresholds: Mapping | HotspotPhaseThresholds | None = None) -> None:
        self.thresholds = thresholds if isinstance(thresholds, HotspotPhaseThresholds) else HotspotPhaseThresholds.from_mapping(thresholds)
        self.phase = "normal"
        self.phase_since_seconds = 0.0
        self.peak_person_count = 0
        self.peak_core_density = 0.0
        self._history: deque[tuple[float, int, float]] = deque()
        self._candidate: str | None = None
        self._candidate_since: float | None = None
        self.transitions: list[dict] = [{"phase": "normal", "time_seconds": 0.0}]

    def update(
        self,
        time_seconds: float,
        person_count: int,
        core_density: float | None,
        *,
        remaining_visitor_count: int = 0,
        blocked_person_count: int = 0,
    ) -> dict:
        now = float(time_seconds)
        count = max(0, int(person_count))
        density = max(0.0, float(core_density or 0.0))
        remaining = max(0, int(remaining_visitor_count))
        blocked = max(0, int(blocked_person_count))
        self.peak_person_count = max(self.peak_person_count, count)
        self.peak_core_density = max(self.peak_core_density, density)
        self._history.append((now, count, density))
        window_start = now - self.thresholds.trend_window_seconds
        while len(self._history) > 1 and self._history[1][0] <= window_start:
            self._history.popleft()

        oldest_time, oldest_count, oldest_density = self._history[0]
        elapsed = max(0.0, now - oldest_time)
        count_change = count - oldest_count
        density_change = density - oldest_density
        count_rate = count_change / elapsed if elapsed else 0.0
        density_rate = density_change / elapsed if elapsed else 0.0
        direction = self._trend_direction(count_rate)

        candidate = self._next_phase(count, density, count_rate, density_rate, remaining, blocked)
        if candidate is None:
            self._candidate = None
            self._candidate_since = None
        elif candidate != self._candidate:
            self._candidate = candidate
            self._candidate_since = now
            if self.thresholds.confirmation_seconds <= 0:
                self._transition(candidate, now)
        elif self._candidate_since is not None and now - self._candidate_since >= self.thresholds.confirmation_seconds:
            self._transition(candidate, now)

        return {
            "phase": self.phase,
            "phase_label": PHASE_LABELS[self.phase],
            "phase_since_seconds": self.phase_since_seconds,
            "reason": self._reason(count, density, count_rate, remaining, blocked),
            "remaining_visitor_count": remaining,
            "blocked_person_count": blocked,
            "trend": {
                "direction": direction,
                "window_seconds": round(elapsed, 3),
                "person_change": count_change,
                "person_rate_per_second": round(count_rate, 3),
                "core_density_change_per_second": round(density_rate, 6),
            },
            "peak_person_count": self.peak_person_count,
            "peak_core_density_person_per_m2": round(self.peak_core_density, 6),
            "transitions": list(self.transitions),
        }

    def _transition(self, phase: str, time_seconds: float) -> None:
        self.phase = phase
        self.phase_since_seconds = time_seconds
        self.transitions.append({"phase": phase, "time_seconds": time_seconds})
        self._candidate = None
        self._candidate_since = None

    def _next_phase(
        self,
        count: int,
        density: float,
        count_rate: float,
        density_rate: float,
        remaining: int,
        blocked: int,
    ) -> str | None:
        thresholds = self.thresholds
        if self.phase == "normal":
            building = count >= thresholds.building_person_count and (
                count_rate >= thresholds.rising_rate_person_per_second
                or density >= thresholds.building_core_density_person_per_m2
            )
            return "building" if building else None
        if self.phase == "building":
            return "congested" if density >= thresholds.congested_core_density_person_per_m2 else None
        if self.phase == "congested":
            below_peak = (
                count <= self.peak_person_count * 0.98
                or density <= self.peak_core_density * 0.95
            )
            falling = count_rate <= thresholds.falling_rate_person_per_second and density_rate < 0
            return "dispersing" if falling and below_peak else None
        if self.phase == "dispersing":
            clear_limit = max(thresholds.cleared_person_count, self.peak_person_count * thresholds.cleared_peak_fraction)
            core_cleared = count <= clear_limit and density <= thresholds.cleared_core_density_person_per_m2
            if not core_cleared:
                return None
            overall_cleared = (
                remaining <= thresholds.cleared_remaining_visitor_count
                and blocked <= thresholds.cleared_blocked_person_count
            )
            if overall_cleared:
                return "cleared"
            if remaining >= thresholds.residual_backlog_person_count or blocked > thresholds.cleared_blocked_person_count:
                return "residual_congestion"
            return None
        if self.phase == "residual_congestion":
            cleared = (
                remaining <= thresholds.cleared_remaining_visitor_count
                and blocked <= thresholds.cleared_blocked_person_count
            )
            return "cleared" if cleared else None
        return None

    def _trend_direction(self, count_rate: float) -> str:
        if count_rate >= self.thresholds.rising_rate_person_per_second:
            return "rising"
        if count_rate <= self.thresholds.falling_rate_person_per_second:
            return "falling"
        return "stable"

    def _reason(self, count: int, density: float, count_rate: float, remaining: int, blocked: int) -> str:
        if self.phase == "normal":
            return f"热点区域当前{count}人，核心密度{density:.2f}人/㎡，尚未形成持续聚集"
        if self.phase == "building":
            return f"热点区域增至{count}人，近窗人数变化率{count_rate:+.2f}人/秒"
        if self.phase == "congested":
            return f"核心密度当前{density:.2f}人/㎡、历史峰值{self.peak_core_density:.2f}人/㎡，拥堵状态已经形成"
        if self.phase == "dispersing":
            return f"热点区域降至{count}人，近窗人数变化率{count_rate:+.2f}人/秒，仍有{remaining}名访客未完成"
        if self.phase == "residual_congestion":
            return f"环道聚集已下降，但仍有{remaining}名访客未完成、{blocked}名行人在公园或入口低速受阻"
        return f"热点区域降至{count}人、核心密度{density:.2f}人/㎡，聚集已基本消散"
