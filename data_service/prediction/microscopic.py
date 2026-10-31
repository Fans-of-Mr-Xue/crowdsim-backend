"""Real JuPedSim movement with a shared, explicit arrival/stay/departure scenario.

The scenario controller is ours; the two operational movement models are not.
Positions use local metres and are aggregated onto the macro solver's cells.
No fallback to NetworkSolver is permitted when the native engine fails.
"""
import math
import time
from datetime import timedelta

import numpy as np
from shapely import points, contains_xy, prepare
from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .arrivals import ArrivalProfile
from .runtime_budget import MAX_PEOPLE, MAX_AGENT_STEPS, MAX_RUNTIME_SECONDS, MAX_SAVED_POSITIONS, runtime_budget

# Resource budgets apply to actual work, never to elapsed research time or
# lifetime arrivals (departed people no longer consume integration work).


class MicroscopicSolver:
    def __init__(self, network, settings, method, start, duration, definition):
        self.net, self.s, self.method = network, settings, method
        self.start, self.duration, self.definition = start, duration, definition
        self.arrivals = ArrivalProfile(settings, duration)
        # The force model needs a finer integration step near narrow walls.
        self.dt = .01 if method == 'social_force' else .05
        if settings['initial_crowd'] > MAX_PEOPLE:
            raise ValueError(f'初始在场人数超过当前逐人计算预算 {MAX_PEOPLE} 人。保留研究时间窗；请核对人数或由管理员提高计算资源预算。')
        try:
            import jupedsim as jps
        except ImportError as exc:
            raise ValueError('尚未安装 JuPedSim 引擎，请按 docs/prediction-prototype.md 安装后重试。') from exc
        if jps.__version__ != '1.4.2':
            raise ValueError('本版适配 JuPedSim 1.4.2，请安装该版本以保证结果可复现。')
        self.jps = jps
        model = jps.CollisionFreeSpeedModel() if method == 'collision_free_speed' else jps.SocialForceModel()
        self.sim = jps.Simulation(model=model, geometry=network['walkable_geometry'], dt=self.dt)
        self.rng = np.random.default_rng(settings['seed'])
        # Separate streams keep external demand independent of movement/spawn success.
        self.demand_rng = np.random.default_rng(settings['seed'] + 1)
        self.radius = settings['agent_radius_m']
        self.safe = network['walkable_geometry'].buffer(-self.radius - .05)
        if self.safe.is_empty:
            raise ValueError('可行走空间不足以容纳所设行人半径，请核对场地宽度。')
        self.cells = network['cell_shapes']
        self.tree = STRtree(self.cells)
        self.n = len(self.cells)
        self.area = network['areas']
        self.core_radius = max(settings['grid_m'], math.sqrt(self.area.sum()) * .22)
        self.core_nodes = set(np.flatnonzero(np.linalg.norm(network['centers'] - network['centers'][network['core']], axis=1) <= self.core_radius))
        self.core_area = unary_union([self.cells[i] for i in self.core_nodes]).intersection(self.safe)
        if self.core_area.is_empty:
            raise ValueError('聚集区域缺少可站立空间，请核对场地几何。')
        self.gate_areas = [self.cells[i].intersection(self.safe) for i in network['gates']]
        if any(area.is_empty for area in self.gate_areas):
            raise ValueError('有通道过窄，无法容纳行人；请核对通道坐标、宽度或行人半径。')
        self.sampling_bounds = {id(area): area.bounds for area in [self.safe, self.core_area, *self.gate_areas]}
        for area in [self.safe, self.core_area, *self.gate_areas]:
            prepare(area)
        self.gate_targets = [tuple(area.representative_point().coords[0]) for area in self.gate_areas]
        # All agents share a steering stage; per-person targets stay distinct.
        # Thousands of departed agents must not leave thousands of live stages.
        self.steering_stage = self.sim.add_direct_steering_stage()
        self.steering_journey = self.sim.add_journey(jps.JourneyDescription([self.steering_stage]))
        self.routing = jps.RoutingEngine(network['walkable_geometry'])
        from .solver import distance_field
        self.gate_distances = np.array([distance_field(network['adjacency'], [gate])[0] for gate in network['gates']])
        self.people = {}
        self.next_person = 0
        self.queues = np.zeros(len(network['gates']), dtype=int)
        self.entry_credit = np.zeros(len(self.queues))
        self.exit_credit = np.zeros(len(self.queues))
        self.departed_by_gate = np.zeros(len(self.queues), dtype=int)
        self.previous_departures = self.departed_by_gate.copy()
        self.requested, self.departed, self.wait_integral = 0, 0, 0.
        self.snapshots = []
        self.agent_steps = self.peak_active = self.saved_positions = 0
        self.started = time.monotonic()
        initial_positions = []
        for _ in range(settings['initial_crowd']):
            position = self.free_position(self.safe, initial_positions, attempts=500)
            if position is None:
                raise ValueError('初始人数无法以不重叠位置放入场地；首版采用均匀随机布置，请降低人数或核对可行走空间。')
            initial_positions.append(position)
            stationary = self.rng.random() < settings['initial_stationary_fraction'] or self.cell_at(position) in self.core_nodes
            self.add_person(position, 0., stationary)

    def point_in(self, area, rng=None):
        rng = rng if rng is not None else self.rng
        lo_x, lo_y, hi_x, hi_y = self.sampling_bounds[id(area)]
        for _ in range(100):
            point = (float(rng.uniform(lo_x, hi_x)), float(rng.uniform(lo_y, hi_y)))
            if contains_xy(area, *point):
                return point
        return tuple(area.representative_point().coords[0])

    def free_position(self, area, occupied, attempts=30):
        occupied = np.asarray(occupied).reshape((-1, 2))
        # Spawn checks only need people near this entrance, not everyone on site.
        clearance = 2 * self.radius + .05
        x0, y0, x1, y1 = self.sampling_bounds[id(area)]
        if len(occupied):
            occupied = occupied[(occupied[:, 0] >= x0 - clearance) & (occupied[:, 0] <= x1 + clearance)
                                & (occupied[:, 1] >= y0 - clearance) & (occupied[:, 1] <= y1 + clearance)]
        for _ in range(attempts):
            point = self.point_in(area)
            if not len(occupied) or np.min(np.linalg.norm(occupied - point, axis=1)) > clearance:
                # JuPedSim validates new agents against its pre-movement index;
                # also check that index so a moving neighbour cannot reject an
                # otherwise clear current-position placement.
                if next(iter(self.sim.agents_in_range(point, 2 * self.radius + 1e-9)), None) is None:
                    return point
        return None

    def cell_at(self, position):
        point = Point(position)
        matches = self.tree.query(point, predicate='intersects')
        if len(matches):
            return int(min(matches))
        raise RuntimeError('行人位置越出可行走空间，本次逐人模拟结果未发布。')

    def cells_at(self, positions):
        """One native spatial query per batch, preserving the original tie rule."""
        if not positions:
            return []
        pairs = self.tree.query(points(positions), predicate='intersects')
        cells = np.full(len(positions), self.n, dtype=int)
        np.minimum.at(cells, pairs[0], pairs[1])
        if np.any(cells == self.n):
            raise RuntimeError('行人位置越出可行走空间，本次逐人模拟结果未发布。')
        return cells.tolist()

    def route(self, position, target):
        """Keep native per-step routing local, including on long straight paths."""
        corners = self.routing.compute_waypoints(position, target)
        path = []
        for start, end in zip(corners, corners[1:]):
            count = max(1, math.ceil(math.dist(start, end) / 2.))
            path.extend([(start[0] + (end[0] - start[0]) * i / count,
                          start[1] + (end[1] - start[1]) * i / count) for i in range(1, count + 1)])
        return path

    def add_person(self, position, seconds, stationary=False):
        # A person's scenario draws do not change when another model takes
        # more spawn attempts or reaches the attraction in a different order.
        behavior = np.random.default_rng(np.random.SeedSequence([self.s['seed'], self.next_person, 2]))
        self.next_person += 1
        dwell = float(behavior.exponential(self.s['dwell_minutes'] * 60))
        route_u = float(behavior.random())
        target = position if stationary else self.point_in(self.core_area, behavior)
        params = dict(position=position, radius=self.radius, desired_speed=self.s['free_speed_mps'], journey_id=self.steering_journey, stage_id=self.steering_stage)
        if self.method == 'collision_free_speed':
            params = self.jps.CollisionFreeSpeedModelAgentParameters(**params, time_gap=self.s['cfs_time_gap_s'])
        else:
            params = self.jps.SocialForceModelAgentParameters(**params, reaction_time=self.s['sfm_reaction_time_s'])
        identifier = self.sim.add_agent(params)
        path = self.route(position, target)
        self.sim.agent(identifier).target = path[0]
        self.people[identifier] = {'scenario_id': self.next_person, 'phase': 'staying' if stationary else 'approaching', 'position': position, 'velocity': (0., 0.),
                                   'release': seconds + dwell, 'dwell': dwell, 'route_u': route_u, 'gate': None, 'path': path, 'waypoint': 0}
        if stationary:
            self.sim.agent(identifier).model.desired_speed = 0.

    def open_gates(self, minute):
        opened = self.s['emergency_open_minute']
        return np.array([gate != 'C' or (opened >= 0 and minute >= opened) for gate in self.net['gate_ids']])

    def step(self, seconds):
        opened = self.open_gates(seconds / 60)
        requested = math.floor(self.arrivals.cumulative(min(self.duration, (seconds + self.dt) / 60)) + 1e-9)
        new = requested - self.requested
        if new:
            self.queues += self.demand_rng.multinomial(new, self.s['entrance_shares'])
        self.requested = requested
        rate = self.s['path_width_m'] * self.s['gate_capacity_per_m_s']
        # No saved throughput credits across idle periods or closed channels.
        limit = max(1., rate * self.dt)
        self.entry_credit = np.where(opened, np.minimum(limit, self.entry_credit + rate * self.dt), 0.)
        self.exit_credit = np.where(opened, np.minimum(limit, self.exit_credit + rate * self.dt), 0.)
        occupied = None
        for gate in range(len(opened)):
            while opened[gate] and self.queues[gate] and self.entry_credit[gate] >= 1 - 1e-9:
                if occupied is None:
                    occupied = [person['position'] for person in self.people.values()]
                point = self.free_position(self.gate_areas[gate], occupied)
                if point is None:
                    break
                if len(self.people) >= MAX_PEOPLE:
                    raise ValueError(f'在研究第 {seconds / 60:.1f} 分钟触及同时在场 {MAX_PEOPLE} 人预算，未截断时间窗或丢弃人员。请核对人数、到达与驻留配置，或提高服务计算预算后在同一任务重跑。')
                self.add_person(point, seconds)
                occupied.append(point)
                self.queues[gate] -= 1
                self.entry_credit[gate] -= 1
        opened_any = opened.any()
        changing = [(identifier, person) for identifier, person in self.people.items()
                    if person['phase'] != 'staying' or (seconds >= person['release'] and opened_any)]
        cells = self.cells_at([person['position'] for _, person in changing])
        for (identifier, person), cell in zip(changing, cells):
            if person['phase'] == 'approaching' and cell in self.core_nodes:
                person['phase'] = 'staying'
                person['release'] = seconds + person['dwell']
                self.sim.agent(identifier).model.desired_speed = 0.
                self.sim.agent(identifier).target = person['position']
            if person['phase'] == 'staying' and seconds >= person['release'] and opened_any:
                costs = self.gate_distances[:, cell] / self.s['free_speed_mps']
                costs[~opened] = np.inf
                weights = np.exp(-np.minimum(700, self.s['route_sensitivity'] * (costs - costs.min())))
                weights[~opened] = 0
                gate = min(len(opened) - 1, int(np.searchsorted(np.cumsum(weights / weights.sum()), person['route_u'], side='right')))
                person.update(phase='departing', gate=gate)
                self.sim.agent(identifier).model.desired_speed = self.s['free_speed_mps']
                person['path'] = self.route(person['position'], self.gate_targets[gate])
                person['waypoint'] = 0
                self.sim.agent(identifier).target = person['path'][0]
            if person['phase'] != 'staying':
                # Follow a cached geometric route. Native CFS/SFM still avoid
                # people/walls at every .05 s step and route to the next waypoint.
                path, waypoint = person['path'], person['waypoint']
                x, y = person['position']
                while waypoint < len(path) - 1 and math.hypot(x - path[waypoint][0], y - path[waypoint][1]) <= 1.:
                    waypoint += 1
                if waypoint != person['waypoint']:
                    person['waypoint'] = waypoint
                    self.sim.agent(identifier).target = path[waypoint]
            gate = person['gate']
            # Match the macro boundary: departure is crossing into the selected
            # absorbing gate cell, subject to the same external service limit.
            if person['phase'] == 'departing' and cell == self.net['gates'][gate] and opened[gate] and self.exit_credit[gate] >= 1 - 1e-9:
                self.sim.mark_agent_for_removal(identifier)
                del self.people[identifier]
                self.departed += 1
                self.departed_by_gate[gate] += 1
                self.exit_credit[gate] -= 1
        self.peak_active = max(self.peak_active, len(self.people))
        self.agent_steps += len(self.people)
        if self.agent_steps > MAX_AGENT_STEPS:
            raise ValueError(f'在研究第 {seconds / 60:.1f} 分钟触及实际在场人数累计计算预算；完整时间窗未完成，未发布该模型的局部结果。请提高服务计算预算后在同一任务重跑。')
        self.sim.iterate()
        for agent in self.sim.agents():
            person = self.people[agent.id]
            position = agent.position
            previous = person['position']
            person['velocity'] = ((position[0] - previous[0]) / self.dt, (position[1] - previous[1]) / self.dt)
            person['position'] = position
        self.wait_integral += int(self.queues.sum()) * self.dt
        if self.s['initial_crowd'] + self.requested != len(self.people) + int(self.queues.sum()) + self.departed:
            raise RuntimeError('逐人模拟违反人数守恒，结果未发布。')

    def snapshot(self, minute):
        self.saved_positions += len(self.people)
        if self.saved_positions > MAX_SAVED_POSITIONS:
            raise ValueError('逐人位置记录超过当前保存预算；请增大输出采样间隔后在同一完整时间窗重跑。采样间隔不会改变模型积分步长。')
        counts = np.zeros(self.n)
        velocity = np.zeros((self.n, 2))
        second = np.zeros(self.n)
        total_speed = np.zeros(self.n)
        directions = np.zeros((self.n, 2))
        moving = np.zeros(self.n)
        agents = []
        cells = self.cells_at([person['position'] for person in self.people.values()])
        for person, cell in zip(self.people.values(), cells):
            v = np.asarray(person['velocity'])
            speed = np.linalg.norm(v)
            counts[cell] += 1
            velocity[cell] += v
            second[cell] += speed ** 2
            total_speed[cell] += speed
            if speed > .01:
                directions[cell] += v / speed
                moving[cell] += 1
            agents.append({'id': person['scenario_id'], 'position': list(self.net['to_wgs84'](*person['position'])), 'phase': person['phase']})
        divisor = np.maximum(1, counts)
        density = counts / self.area
        speed = total_speed / divisor
        variance = np.maximum(0., second / divisor - np.sum((velocity / divisor[:, None]) ** 2, axis=1))
        pressure = density * variance
        conflict = np.where(moving > 0, np.clip(1 - np.linalg.norm(directions, axis=1) / np.maximum(1, moving), 0, 1), 0.)
        risk = np.clip(100 * (1 - np.exp(-(density / self.s['warning_density_p_m2']) ** 2)) + 15 * conflict, 0, 100)
        when = self.start + timedelta(minutes=minute)
        elapsed = minute - self.snapshots[-1]['minute'] if self.snapshots else 0
        flows = (self.departed_by_gate - self.previous_departures) / elapsed if elapsed else np.zeros(len(self.queues))
        self.previous_departures = self.departed_by_gate.copy()
        self.snapshots.append({'minute': round(minute, 4), 'label': when.strftime('%H:%M'), 'timestamp': when.isoformat(),
            'crowd': len(self.people), 'outside_queue': int(self.queues.sum()), 'departed': self.departed, 'cumulative_arrivals': self.requested,
            'density_p_m2': round(float(counts.sum() / self.area.sum()), 4), 'max_density_p_m2': round(float(density.max()), 4),
            'speed_mps': round(float(total_speed.sum() / max(1, counts.sum())), 4), 'max_pressure_s2': round(float(pressure.max()), 6),
            'risk_index': round(float(risk.max()), 2), 'gate_outflows_p_min': np.round(flows, 3).tolist(), 'agents': agents,
            'cells': {key: np.round(value, digits).tolist() for key, value, digits in [('crowd', counts, 3), ('density', density, 4), ('speed', speed, 4),
                         ('pressure', pressure, 6), ('risk', risk, 2), ('conflict', conflict * 100, 2)]}})

    def run(self, progress=None):
        self.snapshot(0.)
        samples = np.arange(self.s['sample_minutes'], self.duration, self.s['sample_minutes']).tolist() + [self.duration]
        step = 0
        for minute in samples:
            target_step = round(minute * 60 / self.dt)
            while step < target_step:
                if step % 100 == 0 and time.monotonic() - self.started > MAX_RUNTIME_SECONDS:
                    raise ValueError(f'逐人计算达到单模型单次 {MAX_RUNTIME_SECONDS // 60} 分钟运行预算（已推进至研究第 {step * self.dt / 60:.1f}/{self.duration:g} 分钟）；完整时间窗未完成，未发布该模型的局部结果。可提高服务资源预算后在同一任务重跑。')
                if progress and step % 100 == 0:
                    progress({'minute': round(step * self.dt / 60, 2), 'duration_minutes': self.duration,
                              'active_agents': len(self.people), 'wall_seconds': round(time.monotonic() - self.started, 1)})
                self.step(step * self.dt)
                step += 1
            self.snapshot(minute)
        peak = max(self.snapshots, key=lambda row: row['max_density_p_m2'])
        warning = next((row for row in self.snapshots if row['max_density_p_m2'] >= self.s['warning_density_p_m2']), None)
        tail = self.snapshots[-1]
        return {'method': self.definition, 'series': self.snapshots, 'rule_trace': [],
            'summary': {'peak_crowd': max(row['crowd'] for row in self.snapshots), 'peak_density_p_m2': peak['max_density_p_m2'], 'peak_time': peak['timestamp'],
                'peak_risk_index': max(row['risk_index'] for row in self.snapshots), 'peak_pressure_s2': max(row['max_pressure_s2'] for row in self.snapshots),
                'max_outside_queue': max(row['outside_queue'] for row in self.snapshots), 'warning_at': warning['timestamp'] if warning else None,
                'departed': self.departed, 'remaining_crowd': tail['crowd'], 'mean_queue_wait_minutes': round(self.wait_integral / max(1, self.requested) / 60, 3), 'mass_balance_error': 0},
            'diagnostics': {'engine': 'JuPedSim', 'engine_version': self.jps.__version__, 'operational_model': type(self.sim.agent(next(iter(self.people))).model).__name__ if self.people else self.method,
                'step_seconds': self.dt, 'cell_count': self.n, 'seed': self.s['seed'], 'population_representation': 'integer_agents',
                'wall_seconds': round(time.monotonic() - self.started, 3), 'actual_agent_steps': self.agent_steps,
                'peak_active_agents': self.peak_active, 'saved_positions': self.saved_positions, 'complete_time_window': True,
                'routing_policy': 'cached_native_waypoints', 'waypoint_advance_distance_m': 1.,
                'max_waypoint_segment_m': 2.,
                'resource_budget': runtime_budget(),
                'arrival_interpolation': self.arrivals.mode, 'expected_new_arrivals': self.arrivals.cumulative(self.duration),
                'realized_new_arrivals': self.requested, 'fractional_arrival_remainder': self.arrivals.cumulative(self.duration) - self.requested,
                'calibration_status': 'scenario_assumptions_not_site_fitted', 'sample_time_tolerance_seconds': self.dt / 2,
                'gate_boundary': 'absorbing_gate_cell_with_external_service_limit', 'initial_velocity_mps': 0,
                'agent_radius_m': self.radius, 'cfs_time_gap_s': self.s['cfs_time_gap_s'] if self.method == 'collision_free_speed' else None,
                'sfm_reaction_time_s': self.s['sfm_reaction_time_s'] if self.method == 'social_force' else None,
                'sfm_defaults': {'mass_kg': 80, 'agent_scale': 2000, 'obstacle_scale': 2000, 'force_distance_m': .08} if self.method == 'social_force' else None}}
