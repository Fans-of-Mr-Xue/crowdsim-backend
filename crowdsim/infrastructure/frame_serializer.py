"""Compatibility serializer from one frozen runtime snapshot."""

from __future__ import annotations

from typing import Any, Dict

from crowdsim.domain.crowdsim_models import AgentState, MotionSnapshot


class FrameSerializer:
    def build_init(self, runtime: Any) -> Dict[str, Any]:
        return {"type": "init", "run_id": runtime.run_id, "center": list(runtime.center), "speedFactor": runtime.sim_speed_factor, "step_length": runtime.step_length, "real_step_interval": runtime.real_step_interval, "flood_points": runtime.flood_points, "flooded_roads": runtime.flooded_roads, "demand": runtime.population.diagnostics(), "metrics": self._metrics(runtime)}

    def build_frame(self, runtime: Any) -> Dict[str, Any]:
        current = runtime.current
        if current is None:
            raise RuntimeError("runtime has no SUMO snapshot")
        pedestrians = [self._pedestrian(runtime, motion) for motion in current.persons.values()]
        vehicles = [{"id": item["id"], "lng": item["lon"], "lat": item["lat"], "speed": round(item["speed"], 3), "color": self._motion_color(item["speed"], 0.0), "flood_impact": 0.0, "congestion": 0.0, "edge": item["edge_id"], "display_edge": item["edge_id"], "synthetic": False, "data_source": "sumo_simulation"} for item in current.vehicles.values()]
        return {"type": "update", "run_id": runtime.run_id, "snapshot_id": runtime.snapshot_id, "step": current.time_seconds, "step_seconds": current.time_seconds, "step_index": runtime.snapshot_index, "step_length": runtime.step_length, "speed_factor": runtime.sim_speed_factor, "real_step_interval": runtime.real_step_interval, "vehicles": vehicles, "pedestrians": pedestrians, "flood_points": runtime.flood_points, "flooded_roads": runtime.flooded_roads, "events": runtime.serialized_events, "metrics": self._metrics(runtime, pedestrians, vehicles), "event_state": runtime.event_state}

    def _pedestrian(self, runtime: Any, motion: MotionSnapshot) -> Dict[str, Any]:
        state = runtime.population.state_for(motion.person_id) or AgentState(motion.person_id)
        profile = runtime.profile_for(motion.person_id)
        congestion = runtime.congestion_for(motion.person_id)
        return {"id": motion.person_id, "lng": motion.lon, "lat": motion.lat, "speed": round(motion.speed, 3), "color": self._motion_color(motion.speed, congestion), "flood_impact": round(runtime.hazard_impact_for(motion.person_id), 3), "congestion": round(congestion, 3), "edge": motion.edge_id, "display_edge": runtime.adapter.display_edge(motion.person_id, motion.edge_id), "synthetic": False, "data_source": "sumo_simulation", "state": {"age_group": profile.age_group, "mobility": round(profile.mobility, 3), "risk_tolerance": round(profile.risk_tolerance, 3), "familiarity": round(profile.familiarity, 3), "group_size": 1 + len(state.companion_ids), "perception_radius": round(profile.perception_radius, 2), "crowding_tolerance": round(profile.crowding_tolerance, 3), "following_tendency": round(profile.following_tendency, 3), "authority_compliance": round(profile.authority_compliance, 3), "stress_susceptibility": round(profile.stress_susceptibility, 3), "group_cohesion": round(profile.group_cohesion, 3), "nearby_people": runtime.local_people_count(motion.person_id), "local_density": runtime.local_density_for(motion.person_id), "density_level": runtime.density_level_for(motion.person_id), "stress": round(state.stress, 3), "perceived_risk": round(state.perceived_risk, 3), "perceived_crowding": round(state.perceived_crowding, 3), "fatigue": round(state.fatigue, 3), "decision": state.current_plan.proposed_action if state.current_plan else "continue", "decision_reason": state.current_plan.reason if state.current_plan else "not_due", "decision_source": state.current_plan.source if state.current_plan else "rule"}}

    @staticmethod
    def _motion_color(speed: float, congestion: float) -> str:
        if congestion >= 0.8:
            return "red"
        if congestion >= 0.5:
            return "orange"
        return "blue" if speed < 0.2 else "green"

    def _metrics(self, runtime: Any, pedestrians=None, vehicles=None) -> Dict[str, Any]:
        pedestrians, vehicles = pedestrians or [], vehicles or []
        combined = pedestrians + vehicles
        average = lambda items: sum(item["speed"] for item in items) / max(1, len(items))
        measured = runtime.latest_metrics
        return {"avg_speed": round(average(combined), 3), "pedestrian_avg_speed": round(measured.get("pedestrian_avg_speed_mps", average(pedestrians)), 3), "vehicle_avg_speed": round(measured.get("vehicle_avg_speed_mps", average(vehicles)), 3), "pedestrian_count": len(pedestrians), "vehicle_count": len(vehicles), "affected": sum(item.get("flood_impact", 0) > 0 for item in pedestrians), "congestion": round(sum(item.get("congestion", 0) for item in pedestrians) / max(1, len(pedestrians)), 3), "water_count": len(runtime.flood_points), "policy": runtime.active_policy, "pedestrian_engine": runtime.adapter.diagnostics, "population": runtime.population.diagnostics(), "decision_engine": runtime.decision_diagnostics, "density_levels": runtime.density_level_counts(), "queue": measured.get("queue", {}), "edge_metrics": measured.get("edges", {}), "units": measured.get("units", {})}
