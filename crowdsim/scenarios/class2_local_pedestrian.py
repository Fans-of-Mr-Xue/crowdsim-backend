"""Automated class-two local-perception decisions with fixed final goals."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import asdict

from shapely.geometry import LineString, Point
from traci import constants as tc

from crowdsim.decision.position_aware_router import EdgePosition, PositionRouteUnavailable
from crowdsim.domain.crowdsim_models import BehaviorPlan
from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
from .class2_flow_layout import flow_network


SYSTEM_PROMPT = """你扮演一名正在步行前往固定目的地的行人。只依据这份个人局部观察作决定。
你知道目的地大致方向，但不知道全场路网、远处拥堵、他人的身份、意图或未来。
附近人员以相对米坐标和速度、朝向表示；道路仅提供感知半径内的片段。
当前采用距离半径感知，尚未模拟建筑遮挡，不能据此推断远处的情况。
目标始终是抵达原目的地。优先保持连续前进；只有观察到受阻或其他局部理由时才改变行为。
continue：沿当前路线以正常期望速度继续；slow_down：以配置速度的60%谨慎前行，最多10仿真秒；
wait：原地等待2仿真秒再恢复；choose_path：选择一个local_options中的局部中间点，终点不变。
候选只说明附近可走方向，不代表全程更快或更安全。没有候选时禁止choose_path。
不要指挥他人、解除干预、修改终点或编造道路。不要持续无理由地等待或反复换路。
仅返回JSON：{"action":"continue|slow_down|wait|choose_path","option_id":null,"reason":"简短的局部依据"}。
仅choose_path的option_id填写候选id，其他动作必须为null。"""


def parse_choice(value, option_ids):
    if not isinstance(value, dict) or set(value) != {'action', 'option_id', 'reason'}:
        raise ValueError('decision must contain only action, option_id and reason')
    action = value['action']
    if action not in {'continue', 'slow_down', 'wait', 'choose_path'}:
        raise ValueError('unsupported local action')
    if action == 'choose_path':
        if not isinstance(value['option_id'], str) or value['option_id'] not in option_ids:
            raise ValueError('unknown local option')
    elif value['option_id'] is not None:
        raise ValueError('non-route action cannot select a waypoint')
    if not isinstance(value['reason'], str) or not value['reason'].strip() or len(value['reason']) > 300:
        raise ValueError('reason must be a short nonempty string')
    return value


class LocalPedestrianModel(LLMDrivenDecisionModel):
    def __init__(self, **kwargs):
        kwargs.setdefault('timeout_seconds', float(os.getenv('CROWDSIM_LLM_DECISION_TIMEOUT', '15')))
        kwargs.setdefault('trust_env', False)
        super().__init__(**kwargs)
        self.base_url = (os.getenv('CROWDSIM_GLM_MAIN_BASE_URL') or
                         f"http://127.0.0.1:{os.getenv('CROWDSIM_GLM_MAIN_LOCAL_PORT', '8800')}/v1").rstrip('/')
        self.model = os.getenv('CROWDSIM_GLM_MAIN_MODEL') or 'GLM-5.3-Flash'
        self.api_key = os.getenv('CROWDSIM_GLM_MAIN_API_KEY') or self.api_key
        # Match the existing server GLM concise-JSON connector. Template flags
        # alone do not disable this endpoint's reasoning token budget.
        self.extra_body.update(enable_thinking=False, reasoning_budget_tokens=0,
                               chat_template_kwargs={'enable_thinking': False})

    async def choose(self, context):
        # Reuse the existing GLM transport, timeout and JSON response contract.
        self.calls += 1
        try:
            value = await self._request([
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)},
            ])
            value = parse_choice(value, {item['id'] for item in context['local_options']})
            self.successes += 1
            return value, None
        except Exception as exc:
            self.fallbacks += 1
            return {'action': 'continue', 'option_id': None, 'reason': '模型不可用，保持原路线'}, type(exc).__name__


class LocalPedestrianController:
    period_seconds = 10.0

    def __init__(self, runtime, model, *, concurrency=4):
        if runtime.class2_flows is None:
            raise ValueError('requires initialized class-two flow runtime')
        self.runtime, self.model = runtime, model
        self.flows = runtime.class2_flows
        self.ids = sorted(set(self.flows.people) - self.flows.background_ids)
        self.final_targets = {pid: dict(self.flows.people[pid]['destination']) for pid in self.ids}
        self.next_round = runtime.time_seconds
        self.concurrency = max(1, concurrency)
        self.previous = {}
        self.slow_until = {}
        self.history = []
        data = flow_network()
        ox, oy = data['originXY']
        self.lanes = [(lane, LineString([(x + ox, y + oy) for x, y in lane['path']])) for lane in data['lanes']]

    def before_tick(self):
        # Replace autonomous rules only for the activity cohort.
        for pid in self.ids:
            self.runtime.population.states[pid].next_decision_time = math.inf
        for pid, deadline in list(self.slow_until.items()):
            if self.runtime.time_seconds >= deadline:
                self.runtime.plan_executor.set_strategy_limit(pid, None)
                del self.slow_until[pid]

    def eligible(self, pid):
        motion = self.runtime.current.persons.get(pid)
        return (motion is not None and pid not in self.flows.at_or_approaching_retention
                and motion.stage_type == tc.STAGE_WALKING and not motion.edge_id.startswith(':'))

    def observe(self, pid):
        """Only this returned context is sent to the model; routes stay private."""
        runtime = self.runtime
        motion, profile = runtime.current.persons[pid], runtime.profile_for(pid)
        radius = profile.perception_radius
        center = Point(motion.x, motion.y)
        region = center.buffer(radius)
        relative = lambda x, y: [round(x - motion.x, 3), round(y - motion.y, 3)]
        neighbours = []
        for other in runtime.current.persons.values():
            if other.person_id != pid and math.hypot(other.x-motion.x, other.y-motion.y) <= radius:
                neighbours.append({'relative_xy_m': relative(other.x, other.y),
                                   'speed_mps': round(other.speed, 3), 'heading_degrees': round(other.angle, 1)})
        roads, visible = [], []
        for lane, line in self.lanes:
            clipped = line.intersection(region)
            parts = [clipped] if clipped.geom_type == 'LineString' else list(getattr(clipped, 'geoms', []))
            for part in parts:
                if part.geom_type != 'LineString' or part.is_empty or part.length < .1:
                    continue
                road_id = f'road-{len(roads)}'
                roads.append({'id': road_id, 'width_m': lane['width'],
                              'relative_path_m': [relative(x, y) for x, y in part.coords]})
                visible.append((road_id, lane, line, part))
        target = self.final_targets[pid]
        dx, dy = target['x']-motion.x, target['y']-motion.y
        norm = max(math.hypot(dx, dy), .001)
        options, routes = self._options(motion, target, radius, visible)
        state = runtime.population.states[pid]
        information = getattr(runtime, 'information', None)
        messages = []
        if information is not None:
            for message_id in state.received_messages:
                message = information.messages.get(message_id)
                if message is not None and message.expires_at > runtime.time_seconds:
                    messages.append({'source': message.source_type, 'content': message.content})
        context = {
            'simulation_seconds': runtime.time_seconds,
            'perception': {'radius_m': radius, 'model': 'distance_radius_without_occlusion',
                           'coordinates': 'relative east/north metres; heading 0=north,90=east'},
            'self': {'role': profile.crowd_role, 'age_band': profile.age_band,
                     'speed_mps': motion.speed, 'heading_degrees': motion.angle,
                     'normal_speed_mps': self.flows.speeds[pid], 'mobility': profile.mobility,
                     'patience': profile.patience, 'crowding_tolerance': profile.crowding_tolerance,
                     'blocked_seconds': state.blocked_duration},
            'goal': {'direction_east_north': [round(dx/norm, 3), round(dy/norm, 3)],
                     'instruction': '前往原定目的地，到达后停留'},
            'nearby_people': neighbours, 'local_roads': roads, 'local_options': options,
            'received_messages': messages,
            'previous_decision': self.previous.get(pid),
        }
        return context, routes

    def _options(self, motion, target, radius, visible):
        router = self.runtime.route_provider.position_router
        source = EdgePosition(motion.edge_id, motion.lane_position)
        goal = EdgePosition(target['edge'], target['position'])
        current_stage = self.runtime.adapter.current_person_stage(motion.person_id)
        current = tuple(current_stage.edges)
        if motion.edge_id in current:
            current = current[current.index(motion.edge_id):]
        seen, options, routes = {current}, [], {}
        # Nearby geometry orders the choices, never global travel times or crowds.
        for road_id, lane, line, part in sorted(visible, key=lambda item: item[3].distance(Point(motion.x, motion.y))):
            if lane['edgeId'] == motion.edge_id:
                continue
            point = part.interpolate(.5, normalized=True)
            offset = (lane['sourceStart'] + line.project(point)) * lane['sourceLength'] / lane['sourceShapeLength']
            waypoint = EdgePosition(lane['edgeId'], offset)
            try:
                first = router.route(source, waypoint)
                if first.distance_m > radius or first.distance_m < .5:
                    continue
                last = router.route(waypoint, goal)
                # An edge-only SUMO walk cannot encode an in-edge U-turn at a waypoint.
                delta = min(.01, offset / 2, (router.edge_lengths[waypoint.edge_id]-offset) / 2)
                if delta <= 0:
                    continue
                ahead = EdgePosition(waypoint.edge_id, offset + delta)
                if (router.route(source, ahead).distance_m-first.distance_m) * (router.route(ahead, goal).distance_m-last.distance_m) >= 0:
                    continue
                edges = first.edges + last.edges[1:]
                if edges in seen or len(set(edges)) != len(edges):
                    continue
                self.runtime.route_provider.validate_edges(edges, current_edge=motion.edge_id)
            except (ValueError, PositionRouteUnavailable):
                continue
            seen.add(edges)
            key = f'option-{len(options)+1}'
            options.append({'id': key, 'road_id': road_id,
                            'waypoint_relative_xy_m': [round(point.x-motion.x, 3), round(point.y-motion.y, 3)]})
            routes[key] = edges
            if len(options) == 3:
                break
        return options, routes

    async def decide_round(self):
        runtime = self.runtime
        if runtime.time_seconds + 1e-8 < self.next_round:
            return []
        now, snapshot_id = runtime.time_seconds, runtime.snapshot_id
        self.next_round = now + self.period_seconds
        prepared = [(pid, *self.observe(pid)) for pid in self.ids if self.eligible(pid)]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def decide(pid, context, routes):
            async with semaphore:
                start = time.perf_counter()
                choice, error = await self.model.choose(context)
                return pid, context, routes, choice, error, time.perf_counter()-start

        results = await asyncio.gather(*(decide(*item) for item in prepared))
        records = []
        for pid, context, routes, choice, error, seconds in results:
            row = {'person_id': pid, 'time': now, 'context': context, 'decision': choice,
                   'fallback_error': error, 'model_seconds': seconds}
            # A caller must not advance/reset the simulation during this round.
            if runtime.snapshot_id != snapshot_id or not self.eligible(pid):
                row['execution'] = {'status': 'discarded', 'reason': 'stale snapshot or arrived'}
            else:
                parse_choice(choice, routes)
                action = choice['action']
                fields = {}
                if action == 'choose_path':
                    fields = {'route_edges': routes[choice['option_id']],
                              'arrival_position': self.final_targets[pid]['position'], 'preserve_future_stages': True}
                elif action == 'slow_down':
                    fields = {'speed_limit': self.flows.speeds[pid] * .6}
                    self.slow_until[pid] = now + self.period_seconds
                elif action == 'wait':
                    fields = {'wait_until': now + 2.0}
                plan = BehaviorPlan(pid, snapshot_id, 'reroute' if action == 'choose_path' else action,
                                    reason=choice['reason'], decided_at=now,
                                    source='local_llm_fallback' if error else 'local_llm', **fields)
                execution = runtime.apply_plan(plan)
                row['execution'] = asdict(execution)
                if execution.status == 'partial_failure':
                    raise RuntimeError(execution.reason)
                self.previous[pid] = {'time': now, 'action': action, 'reason': choice['reason']}
            records.append(row)
        self.history.extend(records)
        return records
