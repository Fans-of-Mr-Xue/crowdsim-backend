"""Time-stepped conservation and stochastic capacity-cell solvers.

Every method consumes the same geometry, demand and controls. There are no stored
forecast curves, peak timestamps or post-cutoff observations in the calculation.
Population is accounted for inside, in external entrance queues, and departed.
"""
import heapq
import math
from datetime import timedelta
import numpy as np
from .catalog import method_catalog
from .custom_formulas import SpeedFormula
from .geometry import build_network
from .inputs import task_times
from .arrivals import ArrivalProfile


def distance_field(adjacency, targets):
    distances = np.full(len(adjacency), np.inf)
    next_node = np.arange(len(adjacency))
    queue = []
    for target in targets:
        distances[target] = 0
        heapq.heappush(queue, (0., target))
    while queue:
        distance, node = heapq.heappop(queue)
        if distance > distances[node]:
            continue
        for other, length in adjacency[node]:
            if distance + length < distances[other]:
                distances[other] = distance + length
                next_node[other] = node
                heapq.heappush(queue, (distance + length, other))
    return distances, next_node


class NetworkSolver:
    def __init__(self, network, settings, method, start, duration, definition):
        self.definition = definition
        self.formula = SpeedFormula(definition['expression'], definition['parameters']) if definition.get('custom') else None
        if not self.formula and method not in ('conservation', 'weidmann', 'stochastic_cells', 'knowledge_network'):
            raise ValueError('该预测方法尚未注册计算实现')
        self.net, self.s, self.method = network, settings, method
        self.start, self.duration = start, duration
        self.arrivals = ArrivalProfile(settings, duration)
        self.rng = np.random.default_rng(settings['seed'])
        self.area = network['areas']
        self.capacity = self.area * settings['jam_density_p_m2']
        self.stochastic = method == 'stochastic_cells'
        if self.stochastic:
            self.capacity = np.floor(self.capacity)
        if settings['initial_crowd'] > self.capacity.sum() * .95:
            raise ValueError(f"初始人数 {settings['initial_crowd']} 超过当前可行走空间可初始化人数 {int(self.capacity.sum() * .95)}；请核对初始人数、道路宽度或可行走空间")
        self.n = len(self.area)
        centers = network['centers']
        radius = max(settings['grid_m'], math.sqrt(self.area.sum()) * .22)
        core_nodes = np.flatnonzero(np.linalg.norm(centers - centers[network['core']], axis=1) <= radius)
        self.core_nodes = core_nodes
        fields = [distance_field(network['adjacency'], core_nodes)]
        fields += [distance_field(network['adjacency'], [gate]) for gate in network['gates']]
        self.distances = np.array([item[0] for item in fields])
        self.targets = np.array([item[1] for item in fields])
        self.lengths = np.maximum(.5, np.linalg.norm(centers[self.targets] - centers, axis=2))
        displacement = centers[self.targets] - centers
        self.directions = displacement / self.lengths[:, :, None]
        for i, gate in enumerate(network['gates']):
            neighbor = network['adjacency'][gate][0][0]
            outward = centers[gate] - centers[neighbor]
            self.directions[i + 1, gate] = outward / np.linalg.norm(outward)
            self.lengths[i + 1, gate] = settings['grid_m'] / 2
        smallest = min(length for rows in network['adjacency'].values() for _, length in rows)
        self.dt = min(2., .8 * smallest / settings['free_speed_mps'])
        if duration * 60 / self.dt > 40000:
            raise ValueError('当前时间窗和网格产生过多时间步，请缩短时间窗或增大计算网格')
        # State rows: approaching, staying, then one departure destination per configured channel.
        self.gate_ids = network['gate_ids']
        self.moving_rows = [0, *range(2, 2 + len(self.gate_ids))]
        self.state = np.zeros((2 + len(self.gate_ids), self.n))
        counts = settings['initial_crowd'] * self.area / self.area.sum()
        if self.stochastic:
            counts = np.floor(counts)
            remaining = settings['initial_crowd'] - int(counts.sum())
            order = np.argsort(-(settings['initial_crowd'] * self.area / self.area.sum() - counts))
            for node in order:
                if remaining and counts[node] < self.capacity[node]:
                    counts[node] += 1; remaining -= 1
                if not remaining:
                    break
            if remaining:
                for node in order:
                    increment = min(remaining, int(self.capacity[node] - counts[node]))
                    counts[node] += increment; remaining -= increment
                    if not remaining:
                        break
        stationary = counts * settings['initial_stationary_fraction']
        if self.stochastic:
            stationary = np.floor(stationary)
        self.state[1] = stationary
        self.state[0] = counts - stationary
        self.state[1, self.core_nodes] += self.state[0, self.core_nodes]
        self.state[0, self.core_nodes] = 0.
        self.queues = np.zeros(len(self.gate_ids))
        self.requested = 0.
        self.departed = 0.
        self.last_speeds = self.speed(counts / self.area)[None, :] * np.linalg.norm(self.directions, axis=2)
        self.last_outflow = np.zeros(len(self.gate_ids))
        self.departed_by_gate = np.zeros(len(self.gate_ids))
        self.previous_departures = self.departed_by_gate.copy()
        self.rule_counts = {'density_shortens_dwell': 0, 'exit_awareness_delay': 0, 'congestion_route_choice': 0}
        self.snapshots = []
        self.arrival_remainder = 0.
        self.wait_integral = 0.
        self.maximum_mass_error = 0.

    def speed(self, density):
        v0, jam = self.s['free_speed_mps'], self.s['jam_density_p_m2']
        if self.formula:
            try:
                return self.formula.evaluate(density, v0, jam)
            except ValueError as exc:
                raise ValueError(f"自定义方法“{self.definition['name']}”：{exc}") from None
        if self.method == 'conservation':
            # Sending demand in a triangular fundamental diagram.
            wave = self.s['backward_wave_speed_mps']
            critical = wave * jam / (v0 + wave)
            return v0 * np.minimum(1., critical / np.maximum(density, 1e-9))
        if self.stochastic:
            return v0 * np.maximum(0., 1. - density / jam)
        return v0 * (-np.expm1(-1.913 * np.maximum(0., 1. / np.maximum(density, 1e-9) - 1. / jam)))

    def route_weights(self, density, minute, is_open):
        costs = self.distances[1:] / self.s['free_speed_mps']
        if self.method == 'knowledge_network':
            # Perceived gate cost includes physical queue, local crowding and recognition.
            costs = costs * (1. + density[None, :] / self.s['warning_density_p_m2'])
            costs += self.queues[:, None] / max(.1, self.s['free_speed_mps'] * self.s['path_width_m'])
            elapsed = minute - self.s['emergency_open_minute'] - self.s['response_delay_minutes']
            awareness = self.s['familiarity']
            if elapsed > 0:
                awareness += (1 - awareness) * self.s['guidance_compliance'] * (1 - math.exp(-elapsed / max(1, self.s['response_delay_minutes'])))
            if 'C' in self.gate_ids:
                controlled = self.gate_ids.index('C')
                costs[controlled] += -math.log(max(.001, awareness)) / self.s['route_sensitivity']
                if is_open[controlled] and awareness < .99:
                    self.rule_counts['exit_awareness_delay'] += 1
            self.rule_counts['congestion_route_choice'] += 1
        beta = self.s['route_sensitivity']
        if not is_open.any():
            return np.zeros_like(costs)
        costs[~is_open] = np.inf
        costs = costs - costs.min(axis=0)
        weights = np.exp(-np.minimum(700, beta * costs))
        weights[~is_open] = 0.
        return weights / np.maximum(weights.sum(axis=0), 1e-12)

    def step(self, minute, dt):
        opened = self.s['emergency_open_minute']
        is_open = np.array([identifier != 'C' or (opened >= 0 and minute >= opened) for identifier in self.gate_ids])
        # Integrate the same curve shown by the editor over this entire time
        # step, rather than sample its left endpoint and change the total mass.
        demand = self.arrivals.integral(minute, minute + dt / 60)
        if self.stochastic:
            self.arrival_remainder += demand
            demand = int(self.arrival_remainder)
            self.arrival_remainder -= demand
            arrivals = self.rng.multinomial(demand, self.s['entrance_shares'])
        else:
            arrivals = demand * np.array(self.s['entrance_shares'])
        self.queues += arrivals; self.requested += demand
        for i, gate in enumerate(self.net['gates']):
            if not is_open[i]:
                continue
            room = max(0., self.capacity[gate] - self.state[:, gate].sum())
            accepted = min(self.queues[i], room, self.s['path_width_m'] * self.s['gate_capacity_per_m_s'] * dt)
            if self.stochastic:
                accepted = math.floor(accepted)
            self.queues[i] -= accepted; self.state[0, gate] += accepted
        # Individuals arriving at the attraction switch state without changing their location.
        self.state[1, self.core_nodes] += self.state[0, self.core_nodes]
        self.state[0, self.core_nodes] = 0.
        count = self.state.sum(axis=0)
        density = count / self.area
        dwell = np.full(self.n, self.s['dwell_minutes'] * 60, dtype=float)
        if self.method == 'knowledge_network':
            above = density > self.s['warning_density_p_m2']
            dwell[above] *= np.maximum(.25, self.s['warning_density_p_m2'] / density[above])
            self.rule_counts['density_shortens_dwell'] += int(above.sum())
        release_probability = -np.expm1(-dt / dwell)
        release = self.rng.binomial(self.state[1].astype(int), release_probability).astype(float) if self.stochastic else self.state[1] * release_probability
        if not is_open.any():
            release[:] = 0.
        weights = self.route_weights(density, minute, is_open)
        self.state[1] -= release
        if self.stochastic:
            for node in np.flatnonzero(release):
                self.state[2:, node] += self.rng.multinomial(int(release[node]), weights[:, node])
        else:
            self.state[2:] += release[None, :] * weights
        moving = self.state[self.moving_rows].copy()
        base_speed = self.speed(density)
        # Opposing streams share one cell's capacity; interaction is not a fixed heatmap weight.
        vector = np.sum(moving[:, :, None] * self.directions, axis=0)
        opposed = 1 - np.linalg.norm(vector, axis=1) / np.maximum(moving.sum(axis=0), 1e-9)
        base_speed *= 1 - self.s['counterflow_penalty'] * np.clip(opposed, 0, 1)
        probability = np.minimum(1., base_speed[None, :] * dt / self.lengths)
        desired = self.rng.binomial(moving.astype(int), probability).astype(float) if self.stochastic else moving * probability
        source, target = np.indices(desired.shape)[1], self.targets
        desired[0, self.core_nodes] = 0.
        for gate_number, gate in enumerate(self.net['gates']):
            if not is_open[gate_number]:
                desired[gate_number + 1] = 0.
            else:
                # Last move crosses the gate, rather than moving back into the same cell.
                desired[gate_number + 1, gate] = min(moving[gate_number + 1, gate], self.s['path_width_m'] * self.s['gate_capacity_per_m_s'] * dt)
                if self.stochastic:
                    desired[gate_number + 1, gate] = math.floor(desired[gate_number + 1, gate])
        leaving = np.zeros_like(desired, dtype=bool)
        for i, gate in enumerate(self.net['gates']):
            leaving[i + 1, gate] = True
        active = (source != target) | leaving
        desired[~active] = 0.
        receive = np.bincount(target[~leaving].ravel(), weights=desired[~leaving].ravel(), minlength=self.n)
        room = np.maximum(0., self.capacity - count)
        if self.method == 'conservation':
            # Backward wave receiving capacity, with equivalent cell cross-section A/L.
            cross_section = self.area / max(1., self.s['grid_m'])
            room = np.minimum(room, self.s['backward_wave_speed_mps'] * np.maximum(0., self.s['jam_density_p_m2'] - density) * cross_section * dt)
        scale = np.minimum(1., room / np.maximum(receive, 1e-9))
        desired[~leaving] *= scale[target[~leaving]]
        if self.stochastic:
            # Stochastic integer rounding and destination arbitration preserve exact counts/capacities.
            desired = np.floor(desired)
            if self.s['friction_probability']:
                claims = np.bincount(target[~leaving].ravel(), weights=(desired[~leaving] > 0).ravel(), minlength=self.n)
                blocked = (claims > 1) & (self.rng.random(self.n) < self.s['friction_probability'])
                desired[~leaving] *= (~blocked[target[~leaving]])
        # Departures and receiving transfers use the SAME accepted flow (no clipping lost people).
        for row, state_row in enumerate(self.moving_rows):
            self.state[state_row] -= desired[row]
            mask = ~leaving[row]
            self.state[state_row] += np.bincount(target[row, mask], weights=desired[row, mask], minlength=self.n)
        self.last_outflow = np.array([desired[i+1, gate] / dt for i, gate in enumerate(self.net['gates'])])
        self.departed_by_gate += self.last_outflow * dt
        self.departed += float(desired[leaving].sum())
        self.last_speeds = desired * self.lengths / np.maximum(moving * dt, 1e-9)
        self.last_speeds = np.minimum(self.s['free_speed_mps'], self.last_speeds)
        self.wait_integral += float(self.queues.sum()) * dt
        error = abs(self.s['initial_crowd'] + self.requested - self.state.sum() - self.queues.sum() - self.departed)
        self.maximum_mass_error = max(self.maximum_mass_error, float(error))
        if error > .001 or self.state.min() < -1e-7 or np.max(self.state.sum(axis=0) - self.capacity) > 1e-6:
            raise RuntimeError('计算违反人数守恒或接收容量约束，结果未发布')

    def snapshot(self, minute):
        counts = self.state.sum(axis=0)
        density = counts / self.area
        moving = self.state[self.moving_rows]
        velocities = self.directions * self.last_speeds[:, :, None]
        mean = np.sum(moving[:, :, None] * velocities, axis=0) / np.maximum(counts[:, None], 1e-9)
        second = np.sum(moving * np.sum(velocities**2, axis=2), axis=0) / np.maximum(counts, 1e-9)
        variance = np.maximum(0., second - np.sum(mean**2, axis=1))
        pressure = density * variance
        moving_count = moving.sum(axis=0)
        conflict = np.where(moving_count > 1e-9, np.clip(1 - np.linalg.norm(np.sum(moving[:, :, None] * self.directions, axis=0), axis=1) / np.maximum(moving_count, 1e-9), 0, 1), 0.)
        risk = np.clip(100 * (1 - np.exp(-(density / self.s['warning_density_p_m2'])**2)) + 15 * conflict, 0, 100)
        speed = np.sum(moving * self.last_speeds, axis=0) / np.maximum(counts, 1e-9)
        time = self.start + timedelta(minutes=minute)
        elapsed = minute - self.snapshots[-1]['minute'] if self.snapshots else 0
        gate_flows = (self.departed_by_gate - self.previous_departures) / elapsed if elapsed else np.zeros(len(self.gate_ids))
        self.previous_departures = self.departed_by_gate.copy()
        result = {'minute': round(minute, 4), 'label': time.strftime('%H:%M'), 'timestamp': time.isoformat(),
            'crowd': round(float(counts.sum()), 2), 'outside_queue': round(float(self.queues.sum()), 2),
            'departed': round(self.departed, 2), 'cumulative_arrivals': round(self.requested, 2),
            'density_p_m2': round(float(counts.sum() / self.area.sum()), 4),
            'max_density_p_m2': round(float(density.max()), 4),
            'speed_mps': round(float(np.sum(counts * speed) / max(1, counts.sum())), 4),
            'max_pressure_s2': round(float(pressure.max()), 6), 'risk_index': round(float(risk.max()), 2),
            'gate_outflows_p_min': np.round(gate_flows, 3).tolist(),
            'cells': {'crowd': np.round(counts, 3).tolist(), 'density': np.round(density, 4).tolist(),
                      'speed': np.round(speed, 4).tolist(), 'pressure': np.round(pressure, 6).tolist(),
                      'risk': np.round(risk, 2).tolist(), 'conflict': np.round(conflict * 100, 2).tolist()}}
        self.snapshots.append(result)

    def run(self):
        self.snapshot(0.)
        seconds, end = 0., self.duration * 60
        next_sample = self.s['sample_minutes'] * 60
        while seconds < end - 1e-7:
            # Stop exactly at demand, control and sample discontinuities.
            boundaries = [end, next_sample]
            boundaries += [row['minute'] * 60 for row in self.s['arrival_schedule'] if row['minute'] * 60 > seconds + 1e-7]
            opening = self.s['emergency_open_minute'] * 60
            if opening > seconds + 1e-7:
                boundaries.append(opening)
            dt = min(self.dt, min(boundaries) - seconds)
            self.step(seconds / 60, dt)
            seconds += dt
            if seconds >= min(next_sample, end) - 1e-7:
                self.snapshot(seconds / 60)
                next_sample += self.s['sample_minutes'] * 60
        warning = self.s['warning_density_p_m2']
        peak = max(self.snapshots, key=lambda item: item['max_density_p_m2'])
        first_warning = next((item for item in self.snapshots if item['max_density_p_m2'] >= warning), None)
        tail = self.snapshots[-1]
        return {'method': self.definition, 'series': self.snapshots,
            'summary': {'peak_crowd': max(item['crowd'] for item in self.snapshots),
                'peak_density_p_m2': peak['max_density_p_m2'], 'peak_time': peak['timestamp'],
                'peak_risk_index': max(item['risk_index'] for item in self.snapshots),
                'peak_pressure_s2': max(item['max_pressure_s2'] for item in self.snapshots),
                'max_outside_queue': max(item['outside_queue'] for item in self.snapshots),
                'warning_at': first_warning['timestamp'] if first_warning else None,
                'departed': tail['departed'], 'remaining_crowd': tail['crowd'],
                'mean_queue_wait_minutes': round(self.wait_integral / max(1, self.requested) / 60, 3),
                'mass_balance_error': round(self.maximum_mass_error, 8)},
            'rule_trace': [{'rule': key, 'activations': value} for key, value in self.rule_counts.items()],
            'diagnostics': {'step_seconds': self.dt, 'cell_count': self.n, 'seed': self.s['seed'],
                'arrival_interpolation': self.arrivals.mode, 'expected_new_arrivals': self.arrivals.cumulative(self.duration),
                'population_representation': 'integer' if self.stochastic else 'continuous_expected_counts',
                'calibration_status': 'scenario_assumptions_not_site_fitted'}}


def compute_comparison(task, roads, settings, methods, progress=None):
    """Picklable CPU worker entry: never receives Mongo clients or HTTP objects."""
    from .repetitions import repetition_seeds, summarize_repetitions
    seeds = repetition_seeds(settings)
    network = build_network(task, roads, settings)
    catalog = {method['id']: method for method in method_catalog(task, include_legacy=True)}
    start, end, duration = task_times(task)
    results, method_errors = [], []
    for method in methods:
        if method not in catalog:
            raise ValueError('当前任务没有所选预测方法的定义')
        repetitions = []
        try:
            for index, seed in enumerate(seeds):
                run_settings = {**settings, 'seed': seed}
                def report(state):
                    if progress:
                        progress({'method': method, 'method_name': catalog[method]['name'], 'repetition': index + 1,
                                  'repetitions': len(seeds), 'duration_minutes': duration, **state})
                report({'minute': 0, 'wall_seconds': 0})
                if method in ('collision_free_speed', 'social_force'):
                    from .microscopic import MicroscopicSolver
                    result = MicroscopicSolver(network, run_settings, method, start, duration, catalog[method]).run(progress=report if progress else None)
                else:
                    result = NetworkSolver(network, run_settings, method, start, duration, catalog[method]).run()
                previous = None
                for row in result['series']:
                    row['outflow_p_min'] = round((row['departed'] - previous['departed']) / (row['minute'] - previous['minute']), 3) if previous else 0.
                    previous = row
                result['diagnostics']['repetition'] = index + 1
                repetitions.append(result)
            result = {**repetitions[0], 'repeat_summary': summarize_repetitions(repetitions)}
            if len(seeds) > 1:
                result['replications'] = repetitions
            results.append(result)
        except (ValueError, RuntimeError) as exc:
            detail = f'第 {len(repetitions) + 1}/{len(seeds)} 次未完成，未将不完整批次纳入比较：' if len(seeds) > 1 else ''
            method_errors.append({'method': catalog[method], 'message': (detail + str(exc))[:600]})
    if not results:
        raise ValueError('；'.join(f"{item['method']['name']}：{item['message']}" for item in method_errors))
    return {'task': task, 'settings': settings, 'road_snapshot': roads, 'network': network['public'], 'results': results,
        'selected_methods': list(methods), 'method_errors': method_errors,
        'time_window': {'start': start.isoformat(), 'end': end.isoformat(), 'timezone': 'Asia/Shanghai'},
        'assumptions': [
            f'每个方法重复 {len(seeds)} 次，按基础种子递增；同一轮各方法使用同一种子。地图与曲线展示所选单次结果，重复统计只汇总数值指标，不平均个体轨迹；确定性守恒模型的多次结果可以相同。',
            '初始人数继承保存的任务参数或本次明确配置；初始空间分布按可行走面积分配，不是视频实况。',
            '未提供到达曲线时使用恒定来流；提供控制点时按所选平滑/阶梯方式积分，各方法使用相同到达需求，不含初始人数。',
            '驻留、路线敏感度、认知、服从与延迟均为情景假设，未使用现场数据拟合。',
            '任务的人口类别与参考案例随运行快照保存；当前模型使用统一自由步速，没有将类别比例自动换成未经依据的速度系数。',
            '主要比较人数、网格密度、排队与离场量；离场流量为相邻切片间的平均值，峰值与预警仅依据保存的切片。',
            '0–100 风险是工程指标，不是事故概率；压力指标为密度乘速度方差，不是人体挤压力。中观方向混杂与逐人实际速度的统计口径不同，压力和冲突仅作探索展示。',
            '分区流量守恒为交通 CTM 的行人适配；CFS 与 SFM 使用 JuPedSim 的真实逐人运动实现，尚未验证哪种更接近本场地实况。',
            '逐人模型初始位置采用不重叠的均匀随机布置、初速为零；整数到达数取累计需求的向下整数，余数小于 1 人。与中观期望人数不完全相等。',
            '逐人模型共用缓存航点引导：新目标确定时由 JuPedSim 规划几何路径，直线段等分为不超过 2 米的子段，距下一航点 1 米内推进；CFS 每 0.05 秒积分，社会力采用更细的 0.01 秒积分。此路径策略与旧版每步重算全程路径不同，版本随结果记录。',
            '三种方法共用入口排队、指数驻留与出口选择假设；出口以吸附网格作为吸收区，并施加配置的通道服务能力，首版没有测绘真实出口断面。',
            '逐人模型以半径限制身体占用；拥挤密度上限仅约束中观模型，也是地图色标上限，不截断逐人模型计算得到的密度。',
        ] + (['自定义公式替换速度—密度关系，仍使用相同的路网流量守恒、入口、驻留和容量约束；表达式与参数随本次结果保存。'] if any(catalog[method].get('custom') for method in methods) else []) + network['public']['warnings']}
