"""Fixed-area observations from actual snapshots, without simulation writes.

Density uses polygon area, not road area. Grid-line points have one cell owner.
Behavior and psychology are observation labels, separate from agent decisions.
"""

from collections import Counter
import hashlib
import math
from pathlib import Path

from traci import constants as tc

from crowdsim.domain.observation_config import (
    BEHAVIOR_STATES, DEFERRED_METRIC_IDS, EVACUATION_METRIC_IDS, IMPLEMENTED_METRIC_IDS,
    OBSERVATION_VERSION, PSYCHOLOGY_STATES, ObservationConfig,
)
from crowdsim.infrastructure.evacuation_metrics import EvacuationTracker


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def psychological_value(value):
    return float(value) if finite(value) and 0 <= value <= 1 else None


class ObservationCollector:
    def __init__(self, scope_xy, metric_ids, config=None, *, source_boundary=None, network_sha256=None, projection=None):
        # Keep geometry optional for legacy runs that have no requirement.
        try:
            from shapely.geometry import Polygon
            from shapely.prepared import prep
        except ImportError as exc:
            raise RuntimeError("observation metrics require shapely; install requirements.txt") from exc
        self.config = config or ObservationConfig.load()
        self.requested_ids = tuple(metric_ids)
        self.metric_ids = tuple(name for name in IMPLEMENTED_METRIC_IDS if name in metric_ids)
        self.computed_ids = set(self.metric_ids)
        if "boundary-density-difference" in self.computed_ids:
            self.computed_ids.add("local-density")
        if self.computed_ids & {"evacuation-efficiency", "absolute-evacuation-density"}:
            self.computed_ids.add("global-density")
        if "absolute-evacuation-density" in self.computed_ids:
            self.computed_ids.add("local-density")
        self.evacuation = EvacuationTracker(self.metric_ids) if self.computed_ids & set(EVACUATION_METRIC_IDS) else None
        self.scope = Polygon(scope_xy)
        if not self.scope.is_valid or self.scope.is_empty or self.scope.area <= 1e-8:
            raise ValueError("observation boundary must be a valid nonzero-area polygon")
        if not all(finite(value) for value in self.scope.bounds):
            raise ValueError("observation boundary coordinates must be finite")
        self.prepared_scope = prep(self.scope)
        self.source_boundary = source_boundary
        self.network_sha256 = network_sha256
        self.projection = projection
        self.cells = {}
        self.boundaries = []
        self._bands = []
        self._band_refs = []
        self._band_tree = None
        self.previous_agents = {}
        self.psych_candidates = {}
        self.avoid_until = {}
        self.sample_count = 0
        self.valid_samples = Counter()
        self.last_time = None
        self._build_geometry()

    @classmethod
    def from_requirement(cls, record, network, config=None):
        if not record:
            return None
        payload = record["requirement"]
        observation = payload["observation"]
        ids = observation["metric_ids"]
        if observation.get("local_partition_mode", "uniform") != "uniform" and any(
            name in ids for name in ("local-density", "local-speed", "boundary-density-difference", "absolute-evacuation-density")
        ):
            raise ValueError("only uniform observation partitions are implemented")
        boundary = payload["spatial_scope"]["boundary"]
        xy = [network.lonlat_to_xy_strict(*point) for point in boundary["coordinates"][0]]
        return cls(xy, ids, config, source_boundary=boundary,
                   network_sha256=hashlib.sha256(Path(network.net_path).read_bytes()).hexdigest(),
                   projection={"crs": network.net.getGeoProj().crs.to_string(),
                               "proj_parameter": network.net.getGeoProj().srs,
                               "net_offset_xy": list(network.net.getLocationOffset()),
                               "sumo_xy_formula": "projected_xy_plus_net_offset"})

    def _build_geometry(self):
        from shapely.geometry import LineString, box
        from shapely.strtree import STRtree
        size = self.config.grid_size_m
        xmin, ymin, xmax, ymax = self.scope.bounds
        self.origin_x = math.floor(xmin / size) * size
        self.origin_y = math.floor(ymin / size) * size
        self.columns = max(1, math.ceil((xmax - self.origin_x) / size))
        self.rows = max(1, math.ceil((ymax - self.origin_y) / size))
        if not self.computed_ids & {"local-density", "local-speed", "boundary-density-difference"}:
            return
        if self.columns * self.rows > self.config.max_grid_cells:
            raise ValueError("observation grid exceeds max_grid_cells; increase grid_size_m")
        for row in range(self.rows):
            for col in range(self.columns):
                x, y = self.origin_x + col * size, self.origin_y + row * size
                geometry = self.scope.intersection(box(x, y, x + size, y + size))
                if geometry.area <= 1e-8:
                    continue
                cell_id = f"cell-r{row:04d}-c{col:04d}"
                self.cells[(col, row)] = {
                    "cell_id": cell_id, "row": row, "column": col,
                    "area_m2": geometry.area, "geometry": geometry,
                    "boundary_ids": {side: [] for side in ("north", "east", "south", "west", "scope")},
                }
        if not math.isclose(sum(cell["area_m2"] for cell in self.cells.values()), self.scope.area, rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError("observation grid does not conserve selected area")
        if "boundary-density-difference" not in self.computed_ids:
            return
        for (col, row), cell in self.cells.items():
            x, y = self.origin_x + col * size, self.origin_y + row * size
            for side, other_side, other_key, path in (
                ("east", "west", (col + 1, row), [(x + size, y), (x + size, y + size)]),
                ("north", "south", (col, row + 1), [(x, y + size), (x + size, y + size)]),
            ):
                other = self.cells.get(other_key)
                if other is None:
                    continue
                line = LineString(path).intersection(cell["geometry"]).intersection(other["geometry"])
                if line.length <= 1e-8:
                    continue
                boundary_id = f'{cell["cell_id"]}:{side}'
                self._add_boundary(boundary_id, cell, other, line, side)
                cell["boundary_ids"][side].append(boundary_id)
                other["boundary_ids"][other_side].append(boundary_id)
            # Irregular clipped edges belong to the outer scope, not an invented
            # fifth neighboring region. No exterior population is observed.
            outer = cell["geometry"].boundary.intersection(self.scope.boundary)
            if outer.length > 1e-8:
                boundary_id = f'{cell["cell_id"]}:scope'
                self._add_boundary(boundary_id, cell, None, outer, "scope")
                cell["boundary_ids"]["scope"].append(boundary_id)
        if self._bands:
            self._band_tree = STRtree(self._bands)

    def _add_boundary(self, boundary_id, first, second, line, direction):
        band = line.buffer(self.config.boundary_band_width_m, cap_style=2, join_style=2)
        index = len(self.boundaries)
        record = {
            "boundary_id": boundary_id, "cell_a": first["cell_id"],
            "cell_b": second["cell_id"] if second else None,
            "direction_from_a": direction, "geometry": line,
            "band_a_area_m2": None, "band_b_area_m2": None,
        }
        for side, cell in (("a", first), ("b", second)):
            if cell is None:
                continue
            geometry = band.intersection(cell["geometry"])
            if geometry.area > 1e-8:
                record[f"band_{side}_area_m2"] = geometry.area
                self._bands.append(geometry)
                self._band_refs.append((index, side, cell["cell_id"]))
        self.boundaries.append(record)

    def geometry_metadata(self):
        from shapely.geometry import mapping
        return {
            **self.config.metadata(), "metric_ids": list(self.metric_ids),
            "requested_metric_ids": list(self.requested_ids),
            "computed_metric_ids": [name for name in IMPLEMENTED_METRIC_IDS if name in self.computed_ids],
            "network_sha256": self.network_sha256,
            "source_crs": "EPSG:4326" if self.source_boundary else None, "source_boundary": self.source_boundary,
            "geometry_crs": "SUMO_XY_METERS", "projection": self.projection, "scope_geometry": mapping(self.scope),
            "area_m2": self.scope.area, "grid_origin_xy": [self.origin_x, self.origin_y],
            "grid_line_ownership": "east_north_cell_with_in_scope_fallback",
            "boundary_difference_sign": "density_a_minus_density_b",
            "outer_boundary_policy": "outside_not_observed",
            "cells": [{**{key: value for key, value in cell.items() if key != "geometry"},
                       "geometry": mapping(cell["geometry"])} for cell in self.cells.values()],
            "boundaries": [{**{key: value for key, value in item.items() if key != "geometry"},
                            "geometry": mapping(item["geometry"])} for item in self.boundaries],
        }

    def _cell_for(self, point):
        size = self.config.grid_size_m
        col = min(self.columns - 1, math.floor((point.x - self.origin_x) / size))
        row = min(self.rows - 1, math.floor((point.y - self.origin_y) / size))
        for dx, dy in ((0, 0), (-1, 0), (0, -1), (-1, -1)):
            cell = self.cells.get((col + dx, row + dy))
            if cell is not None and cell["geometry"].covers(point):
                return cell["cell_id"]
        return None

    def record_execution(self, plan, execution, state, observation=None):
        if "pedestrian-state-change" not in self.metric_ids:
            return
        risk = getattr(observation, "perceived_risk", getattr(state, "perceived_risk", None))
        if execution.status == "applied" and execution.applied_action == "reroute" and (
            finite(risk) and risk >= self.config.avoidance_risk_threshold
        ):
            self.avoid_until[plan.person_id] = execution.applied_at + self.config.avoidance_duration_seconds

    def _behavior(self, motion, state, now):
        if motion.stage_type == tc.STAGE_WAITING or state.activity_state == "hotspot_dwelling" or (
            state.planned_wait_until is not None and state.planned_wait_until > now
        ):
            return "waiting", "actual_waiting_stage_or_planned_stop"
        if finite(motion.speed) and motion.speed < self.config.low_speed_threshold_mps and state.blocked_duration >= self.config.blocked_confirmation_seconds:
            return "blocked", "sustained_unplanned_low_speed"
        if self.avoid_until.get(motion.person_id, -1) > now:
            return "avoiding", "successfully_applied_risk_reroute"
        if motion.stage_type == tc.STAGE_WALKING:
            return "walking", "actual_walking_stage"
        return "unknown", "unclassified_stage"

    def _psychology(self, person_id, stress, now):
        if stress is None:
            self.psych_candidates.pop(person_id, None)
            return "unknown"
        cfg = self.config
        initial = "panic" if stress >= cfg.stress_panic_threshold else "tense" if stress >= cfg.stress_tense_threshold else "calm"
        old = self.previous_agents.get(person_id, {}).get("psychological_state")
        if old in (None, "unknown"):
            return initial
        h = cfg.stress_hysteresis
        if old == "calm":
            candidate = "panic" if stress >= cfg.stress_panic_threshold + h else "tense" if stress >= cfg.stress_tense_threshold + h else "calm"
        elif old == "tense":
            candidate = "panic" if stress >= cfg.stress_panic_threshold + h else "calm" if stress < cfg.stress_tense_threshold - h else "tense"
        else:
            candidate = "calm" if stress < cfg.stress_tense_threshold - h else "tense" if stress < cfg.stress_panic_threshold - h else "panic"
        if candidate == old:
            self.psych_candidates.pop(person_id, None)
            return old
        pending = self.psych_candidates.get(person_id)
        if pending is None or pending[0] != candidate:
            pending = self.psych_candidates[person_id] = (candidate, now)
        if now - pending[1] + 1e-9 >= cfg.psychology_confirmation_seconds:
            self.psych_candidates.pop(person_id, None)
            return candidate
        return old

    def measure(self, step, states, visual_states, snapshot_id, ledger=None):
        from shapely.geometry import Point
        now = step.time_seconds
        if not finite(now) or (self.last_time is not None and now <= self.last_time):
            raise ValueError("observation snapshots require increasing finite simulation time")
        base = {"time_seconds": now, "snapshot_id": snapshot_id}
        counts, speeds, side_counts = Counter(), {}, Counter()
        scoped, agents, transitions = [], {}, []
        invalid_positions = 0
        c1 = "pedestrian-state-change" in self.metric_ids
        c2 = "pedestrian-psychology-change" in self.metric_ids
        for person_id, motion in step.persons.items():
            if not finite(motion.x) or not finite(motion.y):
                invalid_positions += 1
                continue
            point = Point(motion.x, motion.y)
            in_scope = self.prepared_scope.covers(point)
            cell_id = self._cell_for(point) if in_scope and self.cells else None
            if in_scope and self.cells and cell_id is None:
                raise ValueError(f"in-scope pedestrian has no observation cell: {person_id}")
            speed = float(motion.speed) if finite(motion.speed) and motion.speed >= 0 else None
            state = states[person_id]
            indicator = visual_states.get(person_id, {})
            density = indicator.get("density_person_per_m2")
            crowded = (density >= self.config.crowded_density_person_per_m2) if finite(density) and density >= 0 else None
            behavior, behavior_reason = self._behavior(motion, state, now) if c1 else (None, None)
            psychology = self._psychology(person_id, psychological_value(state.stress), now) if c2 else None
            agent = {
                "person_id": person_id, "in_scope": in_scope, "cell_id": cell_id,
                "behavior_state": behavior, "behavior_reason": behavior_reason,
                "crowded": crowded if c1 else None, "psychological_state": psychology,
                "stress": psychological_value(state.stress) if c2 else None,
                "fatigue": psychological_value(state.fatigue) if c2 else None,
                "perceived_risk": psychological_value(state.perceived_risk) if c2 else None,
                "perceived_crowding": psychological_value(state.perceived_crowding) if c2 else None,
                "x": motion.x, "y": motion.y, "edge": motion.edge_id,
            }
            agents[person_id] = agent
            old = self.previous_agents.get(person_id)
            if c1 or c2:
                if in_scope != bool(old and old["in_scope"]):
                    transitions.append({**base, "person_id": person_id, "dimension": "presence",
                        "from_state": "inside" if old and old["in_scope"] else "outside",
                        "to_state": "inside" if in_scope else "outside", "reason": "scope_entry" if in_scope else "scope_exit",
                        "x": motion.x, "y": motion.y, "edge": motion.edge_id, "cell_id": cell_id})
                if in_scope or (old and old["in_scope"]):
                    for field, enabled in (("behavior_state", c1), ("crowded", c1), ("psychological_state", c2)):
                        previous = old.get(field) if old else None
                        if enabled and previous != agent[field]:
                            transitions.append({**base, "person_id": person_id, "dimension": field,
                                "from_state": previous, "to_state": agent[field],
                                "reason": behavior_reason if field == "behavior_state" else "personal_density_threshold" if field == "crowded" else "model_stress_policy",
                                "x": motion.x, "y": motion.y, "edge": motion.edge_id, "cell_id": cell_id})
            if not in_scope:
                continue
            scoped.append((agent, speed))
            if cell_id:
                counts[cell_id] += 1
                if speed is not None:
                    speeds.setdefault(cell_id, []).append(speed)
            if self._band_tree is not None:
                for index in self._band_tree.query(point, predicate="covered_by"):
                    boundary_index, side, owner = self._band_refs[index]
                    if owner == cell_id:
                        side_counts[(boundary_index, side)] += 1
        for person_id, old in self.previous_agents.items():
            if person_id not in agents and old["in_scope"] and (c1 or c2):
                reason = "arrived" if person_id in step.arrived_person_ids else "invalid_position" if person_id in step.persons else "not_in_snapshot"
                transitions.append({**base, "person_id": person_id, "dimension": "presence",
                    "from_state": "inside", "to_state": "absent", "reason": reason,
                    "x": old["x"], "y": old["y"], "edge": old["edge"], "cell_id": old["cell_id"]})
        self.previous_agents = agents if c1 or c2 else {}
        self.psych_candidates = {key: value for key, value in self.psych_candidates.items() if key in agents}
        self.avoid_until = {key: value for key, value in self.avoid_until.items() if key in step.persons and value > now}
        valid_speeds = [speed for _, speed in scoped if speed is not None]
        moving = [speed for speed in valid_speeds if speed > self.config.low_speed_threshold_mps]
        global_row = {
            **base, "person_count": len(scoped), "network_person_count": len(step.persons),
            "outside_scope_person_count": len(step.persons) - len(scoped) - invalid_positions,
            "invalid_position_count": invalid_positions, "area_m2": self.scope.area,
            "density_person_per_m2": len(scoped) / self.scope.area if "global-density" in self.computed_ids else None,
            "speed_sample_count": len(valid_speeds), "invalid_speed_count": len(scoped) - len(valid_speeds),
            "speed_sum_mps": sum(valid_speeds) if "global-speed" in self.computed_ids else None,
            "avg_speed_mps": sum(valid_speeds) / len(valid_speeds) if valid_speeds and "global-speed" in self.computed_ids else None,
            "moving_person_count": len(moving),
            "moving_avg_speed_mps": sum(moving) / len(moving) if moving and "global-speed" in self.computed_ids else None,
        }
        for name in (*BEHAVIOR_STATES, "unknown"):
            global_row[f"behavior_{name}_count"] = sum(agent["behavior_state"] == name for agent, _ in scoped) if c1 else None
        global_row["crowded_person_count"] = sum(agent["crowded"] is True for agent, _ in scoped) if c1 else None
        global_row["crowding_unknown_count"] = sum(agent["crowded"] is None for agent, _ in scoped) if c1 else None
        for name in PSYCHOLOGY_STATES:
            global_row[f"psychology_{name}_count"] = sum(agent["psychological_state"] == name for agent, _ in scoped) if c2 else None
        for field in ("stress", "fatigue", "perceived_risk", "perceived_crowding"):
            values = [agent[field] for agent, _ in scoped if agent[field] is not None]
            global_row[f"{field}_avg"] = sum(values) / len(values) if values else None
        cell_rows = []
        for cell in self.cells.values():
            values = speeds.get(cell["cell_id"], [])
            cell_rows.append({**base, "cell_id": cell["cell_id"], "person_count": counts[cell["cell_id"]],
                "area_m2": cell["area_m2"],
                "density_person_per_m2": counts[cell["cell_id"]] / cell["area_m2"] if "local-density" in self.computed_ids else None,
                "speed_sample_count": len(values),
                "invalid_speed_count": counts[cell["cell_id"]] - len(values),
                "speed_sum_mps": sum(values) if "local-speed" in self.computed_ids else None,
                "avg_speed_mps": sum(values) / len(values) if values and "local-speed" in self.computed_ids else None,
                "low_speed_person_count": sum(value < self.config.low_speed_threshold_mps for value in values) if "local-speed" in self.computed_ids else None})
        boundary_rows = []
        for index, boundary in enumerate(self.boundaries):
            areas = [boundary[f"band_{side}_area_m2"] for side in ("a", "b")]
            densities = [side_counts[(index, side)] / area if area else None for side, area in zip(("a", "b"), areas)]
            difference = densities[0] - densities[1] if all(value is not None for value in densities) else None
            boundary_rows.append({**base, "boundary_id": boundary["boundary_id"],
                "cell_a": boundary["cell_a"], "cell_b": boundary["cell_b"],
                "area_a_m2": areas[0], "area_b_m2": areas[1],
                "person_count_a": side_counts[(index, "a")] if areas[0] else None,
                "person_count_b": side_counts[(index, "b")] if areas[1] else None,
                "density_a_person_per_m2": densities[0], "density_b_person_per_m2": densities[1],
                "difference_person_per_m2": difference,
                "absolute_difference_person_per_m2": abs(difference) if difference is not None else None,
                "status": "valid" if difference is not None else "outside_not_observed" if boundary["cell_b"] is None else "zero_area_band"})
        valid_metric_ids = []
        for name, valid in (
            ("global-density", True), ("global-speed", bool(valid_speeds)),
            ("local-density", bool(cell_rows)), ("local-speed", bool(valid_speeds)),
            ("boundary-density-difference", any(row["status"] == "valid" for row in boundary_rows)),
            ("pedestrian-state-change", bool(scoped)),
            ("pedestrian-psychology-change", any(agent["psychological_state"] not in (None, "unknown") for agent, _ in scoped)),
        ):
            if name in self.computed_ids and valid:
                self.valid_samples[name] += 1
                valid_metric_ids.append(name)
        self.last_time = now
        self.sample_count += 1
        sample = {"schema_version": OBSERVATION_VERSION, **base,
                "metric_ids": list(self.metric_ids), "valid_metric_ids": valid_metric_ids, "global": global_row,
                "cells": cell_rows, "boundaries": boundary_rows,
                "agents": agents, "transitions": transitions}
        if self.evacuation is not None:
            self.evacuation.observe(sample, ledger)
            self.valid_samples.update(name for name in sample["valid_metric_ids"] if name in EVACUATION_METRIC_IDS)
        return sample

    def result(self, *, status, reason, recorded_samples, valid_recorded_samples=None):
        valid_samples = self.valid_samples if valid_recorded_samples is None else valid_recorded_samples
        result = {
            "schema_version": OBSERVATION_VERSION, "status": status, "end_reason": reason,
            "last_sample_time_seconds": self.last_time, "recorded_samples": recorded_samples,
            "metrics": {name: {
                "status": "not_implemented" if name in DEFERRED_METRIC_IDS else "recorded" if recorded_samples and valid_samples[name] else "no_valid_samples",
                "valid_sample_count": valid_samples[name],
            } for name in self.requested_ids},
            "policy": self.config.metadata(),
        }
        if self.evacuation is not None:
            result["evacuation"] = self.evacuation.summary(status, reason)
            for name, metric in result["evacuation"]["metrics"].items():
                result["metrics"][name].update(metric)
        return result
