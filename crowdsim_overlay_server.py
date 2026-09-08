import argparse
import asyncio
import math
import os
import random
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from agent_decision import AgentDecisionEngine
from crowd_environment import CrowdEnvironment
from crowdsim_models import Agent, CrowdEvent, FloodZone, RouteTemplate, Segment
from cultural_profiles import sample_profile
from event_manager import EventManager
from network_adapter import (
    SumoNetAdapter,
    load_pedestrian_routes,
    load_vehicle_routes,
    point_at_distance,
    thin_points,
)
from websocket_server import OverlayServer


PEDNSTREAM_ROOT = os.path.join(os.path.dirname(__file__), "vendor", "PedNStream")
PEDNSTREAM_SRC = os.path.join(PEDNSTREAM_ROOT, "src")
if os.path.isdir(PEDNSTREAM_SRC) and PEDNSTREAM_SRC not in sys.path:
    sys.path.insert(0, PEDNSTREAM_SRC)

try:
    from pednstream.ltm.network import Network as PedNStreamNetwork
except Exception:  # pragma: no cover - reported in runtime diagnostics
    PedNStreamNetwork = None


class PedestrianLtmCore:
    """PedNStream-backed link transmission model for pedestrian network state."""

    def __init__(
        self,
        segments: Dict[str, Segment],
        route_templates: List[RouteTemplate],
        unit_time: float = 2.0,
        simulation_steps: int = 2400,
    ) -> None:
        self.available = PedNStreamNetwork is not None
        self.segments = segments
        self.route_templates = route_templates
        self.unit_time = unit_time
        self.simulation_steps = simulation_steps
        self.current_ltm_step = 0
        self.node_to_idx: Dict[str, int] = {}
        self.idx_to_node: Dict[int, str] = {}
        self.edge_to_pair: Dict[str, Tuple[int, int]] = {}
        self.edge_to_link_key: Dict[str, Tuple[int, int]] = {}
        self.edge_baseline: Dict[str, Dict[str, float]] = {}
        self.origin_nodes: List[int] = []
        self.destination_nodes: List[int] = []
        self.network: Any = None
        self.diagnostics: Dict[str, Any] = {"backend": "pednstream_ltm", "active": False}
        if self.available:
            self._build()

    def _build(self) -> None:
        pedestrian_segments = [seg for seg in self.segments.values() if seg.pedestrian]
        node_ids = sorted({seg.from_node for seg in pedestrian_segments} | {seg.to_node for seg in pedestrian_segments})
        self.node_to_idx = {node_id: idx for idx, node_id in enumerate(node_ids)}
        self.idx_to_node = {idx: node_id for node_id, idx in self.node_to_idx.items()}

        if len(node_ids) < 2:
            self.available = False
            self.diagnostics.update(active=False, reason="not_enough_pedestrian_nodes")
            return

        adjacency = np.zeros((len(node_ids), len(node_ids)), dtype=np.int8)
        link_params: Dict[str, Dict[str, float]] = {}
        pair_to_best_edge: Dict[Tuple[int, int], Segment] = {}

        for seg in pedestrian_segments:
            i = self.node_to_idx[seg.from_node]
            j = self.node_to_idx[seg.to_node]
            if i == j:
                continue
            adjacency[i, j] = 1
            adjacency[j, i] = 1
            pair = (min(i, j), max(i, j))
            current = pair_to_best_edge.get(pair)
            if current is None or seg.length > current.length:
                pair_to_best_edge[pair] = seg
            self.edge_to_pair[seg.id] = (i, j)

        for (i, j), seg in pair_to_best_edge.items():
            width = max(1.2, min(8.0, seg.width))
            params = {
                "length": max(4.0, seg.length),
                "width": width,
                "free_flow_speed": max(0.55, min(1.65, seg.speed)),
                "k_critical": 1.65,
                "k_jam": 5.8,
                "fd_type": "yperman",
                "bi_factor": 1.0,
                "controller_type": "gate",
            }
            link_params[f"{i}_{j}"] = params
            self.edge_baseline[f"{i}_{j}"] = params.copy()

        endpoints = self._extract_route_endpoints()
        origins = [self.node_to_idx[n] for n in endpoints[0] if n in self.node_to_idx]
        destinations = [self.node_to_idx[n] for n in endpoints[1] if n in self.node_to_idx]
        self.origin_nodes = sorted(set(origins))[:48]
        self.destination_nodes = sorted(set(destinations))[:48]
        if not self.origin_nodes:
            self.origin_nodes = sorted({self.node_to_idx[seg.from_node] for seg in pedestrian_segments[:48]})
        if not self.destination_nodes:
            self.destination_nodes = sorted({self.node_to_idx[seg.to_node] for seg in pedestrian_segments[-48:]})

        params = {
            "unit_time": self.unit_time,
            "simulation_steps": self.simulation_steps,
            "assign_flows_type": "classic",
            "default_link": {
                "length": 60.0,
                "width": 2.2,
                "free_flow_speed": 1.25,
                "k_critical": 1.65,
                "k_jam": 5.8,
                "fd_type": "yperman",
                "bi_factor": 1.0,
                "controller_type": "gate",
            },
            "links": link_params,
            "demand": {
                f"origin_{node_id}": {"peak_lambda": 12, "base_lambda": 4}
                for node_id in self.origin_nodes
            },
        }

        try:
            self.network = PedNStreamNetwork(
                adjacency,
                params,
                origin_nodes=self.origin_nodes,
                destination_nodes=[],
                verbose=False,
            )
            for edge_id, (i, j) in self.edge_to_pair.items():
                if (i, j) in self.network.links:
                    self.edge_to_link_key[edge_id] = (i, j)
                elif (j, i) in self.network.links:
                    self.edge_to_link_key[edge_id] = (j, i)
            self.diagnostics.update(
                active=True,
                nodes=len(node_ids),
                links=len(self.network.links),
                origins=len(self.origin_nodes),
                destinations=len(self.destination_nodes),
                unit_time=self.unit_time,
                horizon_steps=self.simulation_steps,
            )
        except Exception as exc:
            self.available = False
            self.network = None
            self.diagnostics.update(active=False, reason=f"{type(exc).__name__}: {exc}")

    def _extract_route_endpoints(self) -> Tuple[List[str], List[str]]:
        origins: List[str] = []
        destinations: List[str] = []
        for route in self.route_templates:
            if not route.edges:
                continue
            first = self.segments.get(route.edges[0])
            last = self.segments.get(route.edges[-1])
            if first is not None:
                origins.append(first.from_node)
            if last is not None:
                destinations.append(last.to_node)
        return origins, destinations

    def configure_demand(self, scale: float) -> None:
        if not self.network:
            return
        for node_id in self.origin_nodes:
            node = self.network.nodes.get(node_id)
            if node is None or getattr(node, "demand", None) is None:
                continue
            base = max(2.0, min(18.0, 2.4 * scale))
            wave = max(base + 3.0, min(36.0, 5.8 * scale))
            values = np.full_like(node.demand, base, dtype=float)
            for center in (180, 520, 980, 1480):
                sigma = 90.0
                indices = np.arange(values.shape[0])
                values += wave * np.exp(-0.5 * ((indices - center) / sigma) ** 2)
            node.demand[:] = values[: node.demand.shape[0]]

    def apply_controls(
        self,
        water_by_edge: Dict[str, float],
        policy: Dict[str, Any],
        relief_by_edge: Optional[Dict[str, float]] = None,
    ) -> None:
        if not self.network:
            return
        speed_recovery = float(policy.get("speed_recovery") or 0.0)
        relief_by_edge = relief_by_edge or {}
        for edge_id, key in self.edge_to_link_key.items():
            link = self.network.links.get(key)
            if link is None:
                continue
            water = max(0.0, min(1.0, water_by_edge.get(edge_id, 0.0)))
            local_relief = max(0.0, min(1.0, relief_by_edge.get(edge_id, 0.0)))
            capacity_multiplier = float(policy.get("capacity_multiplier") or 1.0) * (1.0 + local_relief * 0.75)
            base_width = max(0.4, float(getattr(link, "width", 2.2)))
            baseline_key = f"{min(key)}_{max(key)}"
            baseline = self.edge_baseline.get(baseline_key)
            if baseline:
                base_width = baseline["width"]
            width_factor = max(0.12, 1.0 - water * 0.82) * capacity_multiplier
            recovered = min(base_width, base_width * width_factor + base_width * speed_recovery * 0.22)
            link.front_gate_width = max(0.18, recovered)
            link.back_gate_width = max(0.18, recovered)

    def step_to_time(self, time_seconds: float) -> None:
        if not self.network:
            return
        target_step = min(self.simulation_steps - 1, int(time_seconds / self.unit_time))
        while self.current_ltm_step < target_step:
            self.current_ltm_step += 1
            try:
                self.network.network_loading(self.current_ltm_step)
            except Exception as exc:
                self.available = False
                self.diagnostics.update(active=False, reason=f"network_loading_failed: {type(exc).__name__}: {exc}")
                break

    def edge_state(self, edge_id: str) -> Dict[str, float]:
        if not self.network:
            return {"density": 0.0, "speed": 1.25, "num": 0.0, "congestion": 0.0}
        key = self.edge_to_link_key.get(edge_id)
        if key is None:
            return {"density": 0.0, "speed": 1.25, "num": 0.0, "congestion": 0.0}
        link = self.network.links.get(key)
        if link is None:
            return {"density": 0.0, "speed": 1.25, "num": 0.0, "congestion": 0.0}
        idx = max(0, min(self.current_ltm_step, len(link.density) - 1))
        density = float(link.density[idx])
        speed = float(link.speed[idx]) if idx < len(link.speed) and link.speed[idx] > 0 else float(getattr(link, "free_flow_speed", 1.25))
        num = float(link.num_pedestrians[idx])
        k_critical = max(0.1, float(getattr(link, "k_critical", 1.65)))
        k_jam = max(k_critical + 0.1, float(getattr(link, "k_jam", 5.8)))
        congestion = max(0.0, min(1.6, (density - k_critical * 0.45) / (k_jam - k_critical * 0.45)))
        return {"density": density, "speed": speed, "num": num, "congestion": congestion}


class OverlayNetworkSimulator:
    def __init__(self, net_path: str, ped_routes_path: str, veh_routes_path: str) -> None:
        self.net = SumoNetAdapter(net_path)
        self.net.load()
        self.environment = CrowdEnvironment(self.net.segments)
        self.ped_routes = load_pedestrian_routes(ped_routes_path, self.net.segments)
        self.vehicle_routes = load_vehicle_routes(veh_routes_path, self.net.segments)
        self.ped_routes = self._augment_route_coverage(self.ped_routes, "pedestrian")
        self.vehicle_routes = self._augment_route_coverage(self.vehicle_routes, "vehicle")
        self.ped_ltm = PedestrianLtmCore(self.net.segments, self.ped_routes)
        self.rng = random.Random(20260728)
        self.step_length = 0.5
        self.sim_speed_factor = 1.0
        self.push_fps = 8.0
        self.scale = 3.2
        self.step_index = 0
        self.time_seconds = 0.0
        self.running = False
        self.agents: Dict[str, Agent] = {}
        self.flood_zones: List[FloodZone] = []
        self.event_manager = EventManager(self.net)
        self.decision_engine = AgentDecisionEngine()
        self.decision_interval_steps = 10
        self.max_llm_decisions_per_step = 12
        self.policy: Dict[str, Any] = {
            "name": "",
            "started_step": -1,
            "strength": 0.0,
            "capacity_multiplier": 1.0,
            "speed_recovery": 0.0,
            "water_decay_per_second": 0.0,
            "diversion": 0.0,
        }
        self._spawn_cursor = {"pedestrian": 0, "vehicle": 0}
        self._agent_seq = 0
        self._last_flooded_roads: List[Dict[str, Any]] = []
        self.ped_ltm.configure_demand(self.scale)
        self._ensure_population(initial=True)

    @property
    def center(self) -> Tuple[float, float]:
        return self.net.center_lat, self.net.center_lon

    @property
    def crowd_events(self) -> List[CrowdEvent]:
        return self.event_manager.events

    def configure(self, count: Any = None, speed_factor: Any = None, push_fps: Any = None) -> None:
        if isinstance(count, (int, float)) and count > 0:
            self.scale = max(1.0, min(8.0, float(count) / 2500.0))
        if isinstance(speed_factor, (int, float)) and speed_factor > 0:
            self.sim_speed_factor = max(0.05, min(3.0, float(speed_factor)))
        if isinstance(push_fps, (int, float)) and push_fps > 0:
            self.push_fps = max(2.0, min(30.0, float(push_fps)))
        self.ped_ltm.configure_demand(self.scale)
        self._ensure_population(initial=True)

    def set_flood(self, data: Dict[str, Any]) -> None:
        if data.get("mode") == "clear":
            self.flood_zones = []
            self._last_flooded_roads = []
            return
        if data.get("mode") != "static_points":
            return

        radius_default = safe_float(data.get("radius"), 30.0)
        points = data.get("points", [])
        zones: List[FloodZone] = []
        if isinstance(points, list):
            for item in points:
                if not isinstance(item, dict):
                    continue
                lon = safe_float(item.get("lng", item.get("lon", item.get("longitude"))), math.nan)
                lat = safe_float(item.get("lat", item.get("latitude")), math.nan)
                if not math.isfinite(lon) or not math.isfinite(lat):
                    continue
                depth = max(0.0, min(1.5, safe_float(item.get("depth", item.get("currentDepth")), 0.25)))
                radius = max(8.0, safe_float(item.get("radius"), radius_default))
                x, y = self.net.lonlat_to_xy(lon, lat)
                zones.append(FloodZone(lon=lon, lat=lat, x=x, y=y, depth=depth, radius=radius))
        self.flood_zones = zones
        self._refresh_flooded_roads()

    def set_event(self, data: Dict[str, Any]) -> None:
        self.event_manager.apply(data, self.time_seconds, self.step_index)

    def apply_policy(self, data: Dict[str, Any]) -> None:
        decision = data.get("decision") or data.get("policy") or data.get("name")
        if decision == "police_guidance":
            self.policy.update(
                name=decision,
                started_step=self.step_index,
                strength=0.9,
                capacity_multiplier=2.05,
                speed_recovery=0.48,
                water_decay_per_second=0.030,
                diversion=0.58,
                visual_relief=0.72,
            )
        elif decision == "temporary_diversion":
            self.policy.update(
                name=decision,
                started_step=self.step_index,
                strength=0.82,
                capacity_multiplier=1.65,
                speed_recovery=0.34,
                water_decay_per_second=0.015,
                diversion=0.86,
                visual_relief=0.58,
            )
        elif decision == "observe_only":
            self.policy.update(
                name=decision,
                started_step=self.step_index,
                strength=0.18,
                capacity_multiplier=0.96,
                speed_recovery=0.0,
                water_decay_per_second=0.0,
                diversion=0.04,
                visual_relief=0.0,
            )
        else:
            return

        payload = data.get("payload")
        if isinstance(payload, dict):
            raw_strength = payload.get("containmentLevel", payload.get("strength"))
            if raw_strength is not None:
                self.policy["strength"] = max(0.05, min(1.0, safe_float(raw_strength, 70.0) / 100.0))

    def _policy_relief_factor(self) -> float:
        name = self.policy.get("name")
        if not name or name == "observe_only":
            return 0.0
        started_step = int(self.policy.get("started_step", self.step_index))
        elapsed = max(0.0, (self.step_index - started_step) * self.step_length)
        ramp = min(1.0, elapsed / 5.0)
        strength = float(self.policy.get("strength") or 0.0)
        visual_relief = float(self.policy.get("visual_relief") or 0.0)
        return max(0.0, min(0.88, ramp * strength * visual_relief))

    def _control_influence_on_segment(self, seg: Segment) -> float:
        base_relief = self._policy_relief_factor()
        if base_relief <= 0.0:
            return 0.0
        if not self.flood_zones and not self.crowd_events:
            return base_relief * 0.18

        influence = 0.0
        samples = [point_at_distance(seg.points, seg.length * ratio)[:2] for ratio in (0.18, 0.5, 0.82)]
        for zone in self.flood_zones:
            control_radius = max(zone.radius * 2.2, zone.radius + 180.0)
            for x, y in samples:
                dist = math.hypot(x - zone.x, y - zone.y)
                if dist > control_radius:
                    continue
                falloff = 1.0 - dist / control_radius
                influence = max(influence, base_relief * (0.2 + 0.8 * falloff * falloff))
        for event in self.crowd_events:
            control_radius = max(event.radius * 2.2, event.radius + 120.0)
            for x, y in samples:
                dist = math.hypot(x - event.x, y - event.y)
                if dist <= control_radius:
                    falloff = 1.0 - dist / control_radius
                    influence = max(influence, base_relief * (0.2 + 0.8 * falloff * falloff))
        return max(0.0, min(base_relief, influence))

    def step(self) -> None:
        self.event_manager.step(self.time_seconds)
        if self.policy.get("name") in {"police_guidance", "temporary_diversion"}:
            mitigation = float(self.policy.get("strength") or 0.0) * self.step_length * 0.002
            self.event_manager.mitigate(mitigation)
        self._decay_water()
        self._update_segment_load()
        self._update_agent_perception()
        self._advance_agents()
        self._ensure_population(initial=False)
        self.step_index += 1
        self.time_seconds += self.step_length

    def _target_count(self, kind: str) -> int:
        if kind == "pedestrian":
            return int(max(700, min(3200, 820 * self.scale)))
        return int(max(450, min(2400, 560 * self.scale)))

    def _ensure_population(self, initial: bool) -> None:
        for kind in ("pedestrian", "vehicle"):
            current = sum(1 for agent in self.agents.values() if agent.kind == kind)
            target = self._target_count(kind)
            batch = target - current if initial else min(max(0, target - current), 24 if kind == "pedestrian" else 14)
            for _ in range(max(0, batch)):
                agent = self._spawn_agent(kind, spread=initial)
                if agent is not None:
                    self.agents[agent.id] = agent

    def _augment_route_coverage(self, routes: List[RouteTemplate], kind: str) -> List[RouteTemplate]:
        covered = {edge for route in routes for edge in route.edges}
        compatible = "pedestrian" if kind == "pedestrian" else "vehicle"
        additions: List[RouteTemplate] = []
        for seg in self.net.segments.values():
            if seg.id in covered:
                continue
            if compatible == "pedestrian" and not seg.pedestrian:
                continue
            if compatible == "vehicle" and not seg.vehicle:
                continue
            edges = [seg.id]
            current = seg
            for _ in range(3):
                next_edges = [
                    edge_id for edge_id in current.outgoing
                    if edge_id in self.net.segments
                    and ((compatible == "pedestrian" and self.net.segments[edge_id].pedestrian)
                         or (compatible == "vehicle" and self.net.segments[edge_id].vehicle))
                ]
                if not next_edges:
                    break
                next_edges.sort(key=lambda edge_id: self.net.segments[edge_id].water + self.net.segments[edge_id].congestion)
                edge_id = next_edges[len(additions) % len(next_edges)]
                if edge_id in edges:
                    break
                edges.append(edge_id)
                current = self.net.segments[edge_id]
            additions.append(RouteTemplate(f"coverage_{kind}_{seg.id}", 0.0, edges))
        return routes + additions

    def _spawn_agent(self, kind: str, spread: bool = False) -> Optional[Agent]:
        routes = self.ped_routes if kind == "pedestrian" else self.vehicle_routes
        if not routes:
            return None
        cursor = self._spawn_cursor[kind] % len(routes)
        self._spawn_cursor[kind] += 1
        template = routes[(cursor * 997 + cursor // 11) % len(routes)]
        route = [edge for edge in template.edges if edge in self.net.segments]
        if not route:
            return None

        self._agent_seq += 1
        route_index = self.rng.randrange(0, len(route)) if spread and len(route) > 1 else 0
        first = self.net.segments[route[route_index]]
        profile = sample_profile(self.rng) if kind == "pedestrian" else {}
        base_speed = profile.pop("desired_speed") if kind == "pedestrian" else self.rng.uniform(4.6, 11.5)
        lateral_range = min(2.4, first.width * 0.42) if kind == "pedestrian" else min(1.1, first.width * 0.28)
        return Agent(
            id=f"{'p' if kind == 'pedestrian' else 'v'}_overlay_{self._agent_seq}",
            kind=kind,
            route=route,
            route_index=route_index,
            distance=self.rng.uniform(0.0, max(1.0, first.length * 0.92 if spread else min(first.length * 0.85, 18.0))),
            base_speed=base_speed,
            speed=base_speed,
            lateral=self.rng.uniform(-lateral_range, lateral_range),
            age_group=self.rng.choices(["young", "adult", "senior"], weights=[0.2, 0.65, 0.15])[0],
            mobility=self.rng.uniform(0.65, 1.15),
            risk_tolerance=self.rng.random(),
            familiarity=self.rng.random(),
            group_size=self.rng.choices([1, 2, 3, 4], weights=[0.58, 0.26, 0.11, 0.05])[0],
            perception_radius=self.rng.uniform(8.0, 18.0),
            acceptable_people=self.rng.randint(6, 18),
            next_decision_step=self.rng.randint(0, self.decision_interval_steps),
            **profile,
        )

    def _event_influence_on_segment(self, seg: Segment) -> float:
        return self.environment.event_influence(seg, self.crowd_events)

    def _event_density_multiplier(self, seg: Segment) -> float:
        return self.environment.event_density_multiplier(seg, self.crowd_events)

    def _update_agent_perception(self) -> None:
        self.environment.update_perception(self.agents.values(), self.crowd_events)

    async def resolve_agent_decisions(self) -> None:
        due = [
            agent for agent in self.agents.values()
            if agent.kind == "pedestrian" and self.step_index >= agent.next_decision_step
        ]
        due.sort(key=lambda agent: (agent.density_level != "critical", -agent.stress))
        selected = due[:self.max_llm_decisions_per_step]
        if not selected:
            return
        results = await asyncio.gather(*(self.decision_engine.decide(agent) for agent in selected))
        for agent, (action, reason, source) in zip(selected, results):
            agent.decision = action
            agent.decision_reason = reason
            agent.decision_source = source
            urgency = 4 if agent.density_level == "critical" else self.decision_interval_steps
            agent.next_decision_step = self.step_index + urgency

    def _update_segment_load(self) -> None:
        counts: Dict[str, int] = {}
        for agent in self.agents.values():
            if agent.route_index < len(agent.route):
                edge_id = agent.route[agent.route_index]
                counts[edge_id] = counts.get(edge_id, 0) + 1

        water_by_edge: Dict[str, float] = {}
        relief_by_edge: Dict[str, float] = {}
        for seg in self.net.segments.values():
            local_relief = self._control_influence_on_segment(seg)
            relief_by_edge[seg.id] = local_relief
            capacity_multiplier = float(self.policy.get("capacity_multiplier") or 1.0) * (1.0 + local_relief * 0.75)
            count = counts.get(seg.id, 0)
            event_multiplier = self._event_density_multiplier(seg)
            effective_count = count * event_multiplier
            if count <= 0:
                seg.density = 0.0
                seg.congestion = max(0.0, seg.congestion * 0.72)
            else:
                nominal_capacity = max(4.0, seg.length * max(1.0, seg.width) * 0.12 * capacity_multiplier)
                seg.density = effective_count / max(1.0, seg.length * max(1.0, seg.width))
                target_congestion = max(0.0, min(1.6, effective_count / nominal_capacity))
                seg.congestion = seg.congestion * 0.55 + target_congestion * 0.45
            seg.water = self._water_influence_on_segment(seg)
            event_effects = self.environment.event_effects(seg, self.crowd_events)
            if local_relief > 0.0:
                seg.congestion *= max(0.22, 1.0 - local_relief * 0.72)
                seg.water *= max(0.18, 1.0 - local_relief * 0.55)
            # PedNStream accepts a generic 0..1 capacity impedance despite the
            # historical parameter name `water_by_edge`.
            water_by_edge[seg.id] = max(seg.water, event_effects["speed"])

        self.ped_ltm.apply_controls(water_by_edge, self.policy, relief_by_edge)
        self.ped_ltm.step_to_time(self.time_seconds + self.step_length)
        for seg in self.net.segments.values():
            if not seg.pedestrian:
                continue
            state = self.ped_ltm.edge_state(seg.id)
            seg.congestion = max(seg.congestion * 0.65, state["congestion"])
            if state["density"] > 0:
                seg.density = max(seg.density, state["density"])

    def _advance_agents(self) -> None:
        finished: List[str] = []
        for agent in self.agents.values():
            if agent.route_index >= len(agent.route):
                finished.append(agent.id)
                continue

            seg = self.net.segments.get(agent.route[agent.route_index])
            if seg is None:
                finished.append(agent.id)
                continue

            relief = self._control_influence_on_segment(seg)
            water_penalty = min(0.92, seg.water * (0.84 if agent.kind == "pedestrian" else 1.08) * max(0.18, 1.0 - relief * 0.78))
            congestion_penalty = min(0.78, max(0.0, seg.congestion - 0.52) * (0.52 if agent.kind == "pedestrian" else 0.46) * max(0.2, 1.0 - relief * 0.86))
            recovery = float(self.policy.get("speed_recovery") or 0.0)
            if self.policy.get("name") == "observe_only":
                recovery = 0.0
                congestion_penalty = min(0.88, congestion_penalty + 0.08)

            speed_factor = max(0.08, 1.0 - water_penalty - congestion_penalty + recovery + relief * 0.22)
            if agent.kind == "pedestrian":
                event_effects = self.environment.event_effects(seg, self.crowd_events)
                action_factor = {
                    "continue": 1.0,
                    "slow_down": 0.62,
                    "avoid": 0.82,
                    "follow_crowd": 0.9,
                    "wait": 0.08,
                }.get(agent.decision, 1.0)
                speed_factor *= action_factor * agent.mobility
                speed_factor *= max(0.08, 1.0 - event_effects["speed"])
                agent.fatigue = min(1.0, agent.fatigue + self.step_length * (0.001 + agent.stress * 0.002))
                speed_factor *= max(0.65, 1.0 - agent.fatigue * 0.25)
            target_speed = agent.base_speed * speed_factor
            if agent.kind == "pedestrian":
                ltm_state = self.ped_ltm.edge_state(seg.id)
                target_speed = min(target_speed, max(0.08, ltm_state["speed"] * (1.0 - water_penalty * 0.35 + recovery * 0.18)))
            target_speed = min(target_speed, seg.speed * (0.86 if agent.kind == "pedestrian" else 1.0))
            min_speed = 0.22 if agent.kind == "pedestrian" and water_penalty < 0.65 else (0.08 if agent.kind == "pedestrian" else 0.4)
            target_speed = max(min_speed, target_speed)

            agent.speed = agent.speed * 0.65 + target_speed * 0.35
            agent.low_speed_ticks = agent.low_speed_ticks + 1 if agent.speed < min_speed * 1.2 else 0
            if agent.low_speed_ticks > 24 and water_penalty < 0.75:
                replacement = self._spawn_agent(agent.kind, spread=True)
                if replacement is not None:
                    agent.route = replacement.route
                    agent.route_index = replacement.route_index
                    agent.distance = replacement.distance
                    agent.base_speed = replacement.base_speed
                    agent.speed = replacement.speed
                    agent.lateral = replacement.lateral
                    agent.low_speed_ticks = 0
                    seg = self.net.segments.get(agent.route[agent.route_index], seg)
            agent.flood_impact = max(0.0, seg.water * max(0.16, 1.0 - relief * 0.84))
            agent.congestion = max(0.0, seg.congestion * max(0.2, 1.0 - relief * 0.9))
            agent.color = self._agent_color(agent)

            agent.distance += agent.speed * self.step_length
            while agent.route_index < len(agent.route):
                current = self.net.segments.get(agent.route[agent.route_index])
                if current is None:
                    finished.append(agent.id)
                    break
                if agent.distance < current.length:
                    break
                agent.distance -= current.length
                agent.route_index += 1
                if agent.route_index < len(agent.route):
                    self._maybe_divert(agent, force=agent.kind == "pedestrian" and (agent.decision == "avoid" or agent.event_avoidance >= 0.35))
                else:
                    finished.append(agent.id)
                    break

        for agent_id in finished:
            self.agents.pop(agent_id, None)

    def _maybe_divert(self, agent: Agent, force: bool = False) -> None:
        diversion = float(self.policy.get("diversion") or 0.0)
        if not force and (diversion <= 0.01 or self.rng.random() > diversion):
            return
        current_id = agent.route[agent.route_index]
        current = self.net.segments.get(current_id)
        if current is None or not current.outgoing:
            return

        candidates = [
            edge_id for edge_id in current.outgoing
            if edge_id in self.net.segments
            and ((agent.kind == "pedestrian" and self.net.segments[edge_id].pedestrian)
                 or (agent.kind == "vehicle" and self.net.segments[edge_id].vehicle))
        ]
        if not candidates:
            return
        candidates.sort(key=lambda edge_id: self.net.segments[edge_id].water * 1.6 + self.net.segments[edge_id].congestion)
        if candidates[0] != current_id and (force or self.rng.random() < 0.65):
            agent.route = agent.route[:agent.route_index] + [candidates[0]] + agent.route[agent.route_index + 1:]

    def _agent_color(self, agent: Agent) -> str:
        if agent.flood_impact >= 0.18:
            return "blue"
        if agent.congestion >= 0.95:
            return "red"
        if agent.congestion >= 0.66:
            return "orange"
        if agent.speed < (0.32 if agent.kind == "pedestrian" else 1.2):
            return "blue"
        return "green"

    def _water_influence_on_segment(self, seg: Segment) -> float:
        if not self.flood_zones:
            return 0.0
        influence = 0.0
        sample_count = min(5, len(seg.points))
        if sample_count <= 2:
            samples = seg.points
        else:
            samples = [point_at_distance(seg.points, seg.length * i / (sample_count - 1))[:2] for i in range(sample_count)]
        for zone in self.flood_zones:
            for x, y in samples:
                dist = math.hypot(x - zone.x, y - zone.y)
                radius = max(6.0, zone.radius + zone.depth * 22.0)
                if dist > radius:
                    continue
                falloff = 1.0 - dist / radius
                influence = max(influence, min(1.0, zone.depth * falloff * falloff))
        return influence

    def _decay_water(self) -> None:
        decay = float(self.policy.get("water_decay_per_second") or 0.0)
        if decay <= 0.0 or not self.flood_zones:
            return
        relief = self._policy_relief_factor()
        for zone in self.flood_zones:
            effective_decay = decay * (1.0 + relief * 3.2)
            zone.depth = max(0.0, zone.depth - effective_decay * self.step_length)
            zone.radius = max(8.0, zone.radius - effective_decay * self.step_length * 28.0)
        self.flood_zones = [zone for zone in self.flood_zones if zone.depth > 0.015]
        self._refresh_flooded_roads()

    def _refresh_flooded_roads(self) -> None:
        roads: List[Tuple[float, Segment]] = []
        for seg in self.net.segments.values():
            influence = self._water_influence_on_segment(seg)
            if influence >= 0.08:
                roads.append((influence, seg))
        roads.sort(key=lambda item: item[0], reverse=True)
        self._last_flooded_roads = [
            {
                "id": seg.id,
                "impact": round(impact, 3),
                "shape": [[self.net.xy_to_lonlat(x, y)[1], self.net.xy_to_lonlat(x, y)[0]] for x, y in thin_points(seg.points, 8)],
            }
            for impact, seg in roads[:120]
        ]

    def frame(self) -> Dict[str, Any]:
        vehicles: List[Dict[str, Any]] = []
        pedestrians: List[Dict[str, Any]] = []
        total_speed = 0.0
        total_count = 0
        affected = 0
        congestion_sum = 0.0

        for agent in self.agents.values():
            seg = self.net.segments.get(agent.route[agent.route_index]) if agent.route_index < len(agent.route) else None
            if seg is None:
                continue
            x, y, ux, uy = point_at_distance(seg.points, agent.distance)
            x -= uy * agent.lateral
            y += ux * agent.lateral
            lon, lat = self.net.xy_to_lonlat(x, y)
            item = {
                "id": agent.id,
                "lng": lon,
                "lat": lat,
                "speed": round(agent.speed, 3),
                "color": agent.color,
                "flood_impact": round(agent.flood_impact, 3),
                "congestion": round(agent.congestion, 3),
                "edge": seg.id,
                "synthetic": False,
            }
            if agent.kind == "pedestrian":
                item["state"] = {
                    "age_group": agent.age_group,
                    "cultural_group": agent.cultural_group,
                    "mobility": round(agent.mobility, 3),
                    "risk_tolerance": round(agent.risk_tolerance, 3),
                    "familiarity": round(agent.familiarity, 3),
                    "group_size": agent.group_size,
                    "perception_radius": round(agent.perception_radius, 2),
                    "acceptable_people": agent.acceptable_people,
                    "density_tolerance": round(agent.density_tolerance, 2),
                    "following_tendency": round(agent.following_tendency, 3),
                    "authority_compliance": round(agent.authority_compliance, 3),
                    "panic_susceptibility": round(agent.panic_susceptibility, 3),
                    "group_cohesion": round(agent.group_cohesion, 3),
                    "stress_response": agent.stress_response,
                    "nearby_people": agent.nearby_people,
                    "local_density": round(agent.local_density, 3),
                    "density_level": agent.density_level,
                    "stress": round(agent.stress, 3),
                    "event_impact": round(agent.event_impact, 3),
                    "event_avoidance": round(agent.event_avoidance, 3),
                    "event_phase": agent.event_phase,
                    "fatigue": round(agent.fatigue, 3),
                    "decision": agent.decision,
                    "decision_reason": agent.decision_reason,
                    "decision_source": agent.decision_source,
                }
                pedestrians.append(item)
            else:
                vehicles.append(item)
            total_speed += agent.speed
            congestion_sum += agent.congestion
            total_count += 1
            if agent.flood_impact >= 0.08 or agent.congestion >= 0.72:
                affected += 1

        return {
            "type": "update",
            "step": self.time_seconds,
            "step_seconds": self.time_seconds,
            "step_index": self.step_index,
            "step_length": self.step_length,
            "speed_factor": self.sim_speed_factor,
            "real_step_interval": self.step_length / max(0.01, self.sim_speed_factor),
            "vehicles": vehicles,
            "pedestrians": pedestrians,
            "flood_points": [
                {"lng": z.lon, "lat": z.lat, "depth": z.depth, "radius": z.radius}
                for z in self.flood_zones
            ],
            "flooded_roads": self._last_flooded_roads,
            "events": [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "phase": event.phase,
                    "radius": event.radius,
                    "intensity": event.intensity,
                    "density_multiplier": event.density_multiplier,
                    "effects": {
                        "speed": event.speed_impact,
                        "stress": event.stress_impact,
                        "avoidance": event.avoidance_pressure,
                    },
                    "source": event.source,
                    "remaining_seconds": max(0.0, event.expires_at - self.time_seconds),
                }
                for event in self.crowd_events
            ],
            "metrics": {
                "avg_speed": round(total_speed / max(1, total_count), 3),
                "affected": affected,
                "congestion": round(congestion_sum / max(1, total_count), 3),
                "policy": self.policy.get("name", ""),
                "water_count": len(self.flood_zones),
                "pedestrian_engine": self.ped_ltm.diagnostics,
                "decision_engine": self.decision_engine.diagnostics(),
                "density_levels": self.environment.density_counts(self.agents.values()),
            },
            "event_state": {
                "crowd_gathering": {
                    "active": bool(self.policy.get("name")),
                    "decision": self.policy.get("name", ""),
                    "step": self.policy.get("started_step", -1),
                }
            },
        }


def safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def default_scenario_dir() -> str:
    start = os.path.abspath(os.path.dirname(__file__))
    local = os.path.join(start, "scenarios", "shanghai_bund")
    if os.path.exists(os.path.join(local, "bund.net.xml")):
        return local
    for parent in [start, *iter_parents(start)]:
        candidate = os.path.join(parent, "backend", "scenarios", "shanghai_bund")
        if os.path.exists(os.path.join(candidate, "bund.net.xml")):
            return candidate
    return os.path.abspath(os.path.join(start, "..", "..", "..", "backend", "scenarios", "shanghai_bund"))


def iter_parents(path: str) -> Iterable[str]:
    current = os.path.abspath(path)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            break
        yield parent
        current = parent


def parse_args() -> argparse.Namespace:
    scenario_dir = default_scenario_dir()
    parser = argparse.ArgumentParser(description="Road-network overlay crowd simulator for the CrowdSim frontend.")
    parser.add_argument("--net", default=os.path.join(scenario_dir, "bund.net.xml"))
    parser.add_argument("--ped-routes", default=os.path.join(scenario_dir, "bund_ped.rou.xml"))
    parser.add_argument("--veh-routes", default=os.path.join(scenario_dir, "bund_veh.rou.xml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    simulator = OverlayNetworkSimulator(args.net, args.ped_routes, args.veh_routes)
    server = OverlayServer(simulator, args.host, args.port)
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
