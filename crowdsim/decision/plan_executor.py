"""Validate behavior plans and apply only explicit TraCI person operations."""

from __future__ import annotations

from typing import Dict, Optional

from traci import constants as tc
from traci._simulation import Stage

from crowdsim.domain.crowdsim_models import AgentProfile, BehaviorPlan, MotionSnapshot, PlanExecutionResult
from crowdsim.decision.route_provider import RouteProvider
from crowdsim.infrastructure.sumo_adapter import SumoAdapter


EXECUTABLE_ACTIONS = {"continue", "slow_down", "wait", "reroute", "change_goal"}


class PlanExecutor:
    def __init__(self, adapter: SumoAdapter, route_provider: RouteProvider) -> None:
        self.adapter = adapter
        self.route_provider = route_provider
        self.strategy_limits: Dict[str, float] = {}
        self.hazard_limits: Dict[str, float] = {}
        self.wait_until: Dict[str, float] = {}
        self.base_limits: Dict[str, float] = {}
        self.results: Dict[str, PlanExecutionResult] = {}

    def register_profile(self, profile: AgentProfile) -> None:
        self.base_limits[profile.person_id] = max(0.0, profile.free_walking_speed * profile.mobility)

    def validate_plan(self, plan: BehaviorPlan, motion: MotionSnapshot, snapshot_id: str, now: float) -> None:
        if plan.person_id != motion.person_id:
            raise ValueError("plan person_id does not match motion snapshot")
        if plan.snapshot_id != snapshot_id:
            raise ValueError("stale plan snapshot_id")
        if plan.expires_at is not None and plan.expires_at < now:
            raise ValueError("plan has expired")
        if plan.proposed_action not in EXECUTABLE_ACTIONS:
            raise ValueError(f"action must be reduced before execution: {plan.proposed_action}")
        if plan.proposed_action == "slow_down" and (plan.speed_limit is None or plan.speed_limit < 0):
            raise ValueError("slow_down requires a non-negative speed_limit")
        if plan.proposed_action == "wait" and (plan.wait_until is None or plan.wait_until <= now):
            raise ValueError("wait requires a future wait_until")
        if plan.proposed_action in {"reroute", "change_goal"}:
            self.route_provider.validate_edges(plan.route_edges, current_edge=motion.edge_id)
            if plan.next_route_edges:
                self.route_provider.validate_edges((*plan.route_edges[-1:], *plan.next_route_edges))
            if plan.activity_duration is not None and plan.activity_duration <= 0:
                raise ValueError("activity_duration must be positive")

    def apply(self, plan: BehaviorPlan, motion: MotionSnapshot, snapshot_id: str, now: float) -> PlanExecutionResult:
        try:
            self.validate_plan(plan, motion, snapshot_id, now)
            action = plan.proposed_action
            if action == "continue":
                self.strategy_limits.pop(plan.person_id, None)
                self.wait_until.pop(plan.person_id, None)
                self._apply_effective_speed(plan.person_id)
            elif action == "slow_down":
                self.strategy_limits[plan.person_id] = float(plan.speed_limit)
                self._apply_effective_speed(plan.person_id)
            elif action == "wait":
                self.strategy_limits[plan.person_id] = 0.0
                self.wait_until[plan.person_id] = float(plan.wait_until)
                self._apply_effective_speed(plan.person_id)
            else:
                current_stage = self.adapter.current_person_stage(plan.person_id)
                stage = Stage(
                    type=tc.STAGE_WALKING,
                    edges=list(plan.route_edges),
                    departPos=motion.lane_position,
                    arrivalPos=current_stage.arrivalPos,
                    description=f"crowdsim:{action}:{plan.target_id or ''}",
                )
                self.adapter.replace_current_person_stage(plan.person_id, stage)
                if action == "change_goal" and plan.activity_duration is not None:
                    if not plan.next_route_edges:
                        raise ValueError("activity goal requires a preplanned next walking stage")
                    self.adapter.append_waiting_stage(plan.person_id, plan.activity_duration, f"activity:{plan.target_id or ''}")
                    next_edge = self.route_provider.network.edges[plan.next_route_edges[-1]]
                    self.adapter.append_walking_stage(plan.person_id, plan.next_route_edges, next_edge.getLength())
                self._apply_effective_speed(plan.person_id)
            result = PlanExecutionResult(plan.person_id, action, action, "applied", now, plan.reason)
        except Exception as exc:
            result = PlanExecutionResult(plan.person_id, plan.proposed_action, "continue", "rejected", now, f"{type(exc).__name__}: {exc}")
        self.results[plan.person_id] = result
        return result

    def maintain(self, now: float) -> None:
        expired = [person_id for person_id, deadline in self.wait_until.items() if now >= deadline]
        for person_id in expired:
            self.wait_until.pop(person_id, None)
            self.strategy_limits.pop(person_id, None)
            self._apply_effective_speed(person_id)

    def set_hazard_limit(self, person_id: str, limit: Optional[float]) -> None:
        if limit is None:
            self.hazard_limits.pop(person_id, None)
        else:
            self.hazard_limits[person_id] = max(0.0, float(limit))
        self._apply_effective_speed(person_id)

    def effective_speed(self, person_id: str) -> float:
        values = [self.base_limits.get(person_id, 1.35)]
        if person_id in self.strategy_limits:
            values.append(self.strategy_limits[person_id])
        if person_id in self.hazard_limits:
            values.append(self.hazard_limits[person_id])
        return min(values)

    def _apply_effective_speed(self, person_id: str) -> None:
        self.adapter.set_person_speed(person_id, self.effective_speed(person_id))
