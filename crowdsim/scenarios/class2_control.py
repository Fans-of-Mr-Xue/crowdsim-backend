"""Scheduled controls for one confirmed class-two simulation run."""
from collections import Counter
from copy import deepcopy
from math import floor, isclose, isfinite
import re

import traci.constants as tc
from traci.exceptions import TraCIException

from crowdsim.control.inflow_meter import InflowMeter
from crowdsim.decision.position_aware_router import EdgePosition, PositionRouteUnavailable
from crowdsim.domain.crowdsim_models import BehaviorPlan
from crowdsim.environment.information_model import InformationMessage
from .class2_flow_layout import flow_network, position
from .class2_strategy_tool import ACTIONS, INTERVAL_ACTIONS, MAX_ACTIONS, parse_action_lines, strategy_tool


GATES = {'178411801#0': 'M1 西侧入口', '177931018#0': 'M2 南侧入口', 'monument_m3': 'M3 西侧中部入口'}
RING_EDGES = {'679361567#1', '679361567#2', '679361567#2_m3_north'}
IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,160}')
EPSILON = 1e-8


def number(value, low, high, label):
    if type(value) not in (int, float) or not low <= value <= high or not isfinite(value):
        raise ValueError(f'{label}超出范围')
    return float(value)


class Class2Control:
    def __init__(self, flows):
        self.flows = flows
        self.members = {}
        for pid, person in flows.people.items():
            self.members.setdefault(person['flow_id'], []).append(pid)
        for members in self.members.values():
            members.sort()
        lanes = {lane['id']: lane for lane in flow_network()['lanes']}
        self.edges = {lane['edgeId'] for lane in lanes.values()}
        self.flow_aliases = {f'F{i}': flow['id'] for i, flow in enumerate(flows.layout['flows'], 1)}
        self.origins = {}
        for flow in flows.layout['flows']:
            ranges = self.origins[flow['id']] = {}
            for span in flow['origin_layout']['ranges']:
                lane = lanes[span['lane_id']]
                a, b = (position(lane, span[key])['position'] for key in ('start', 'end'))
                ranges.setdefault(lane['edgeId'], []).append((min(a, b), max(a, b)))
        self.plan = None
        self.rows = {}

    def binding(self, runtime):
        source = runtime.requirement_record['requirement']['class2_demo']['source']
        return {**source, 'runId': runtime.run_id, 'frameId': runtime.snapshot_id,
                'requirementId': runtime.requirement_record['requirement_id'], 'timeSeconds': runtime.time_seconds}

    def context(self, runtime):
        from .class2_control_context import summarize_paths

        eligible = self.origin_people(runtime)
        state = runtime.state.value
        available = set(ACTIONS) if state in {'READY', 'PAUSED'} and runtime.time_seconds < runtime.timeline_end_seconds else set()
        if not eligible:
            available.discard('release_flow')
        return {'source': self.binding(runtime), 'runtimeState': state, 'stepSeconds': runtime.step_length,
                'endSeconds': runtime.timeline_end_seconds, 'maxActions': MAX_ACTIONS,
                'actions': sorted(available),
                'actionTool': strategy_tool(available, self.flow_aliases),
                'entrances': [{'edgeId': edge, 'name': name} for edge, name in GATES.items()],
                'edgeIds': sorted(self.edges), 'originWaitingCounts': dict(Counter(self.flows.people[p]['flow_id'] for p in eligible)),
                **summarize_paths(runtime, self.flows)}

    def origin_people(self, runtime):
        eligible = set()
        for pid, motion in runtime.current.persons.items():
            person = self.flows.people[pid]
            spans = self.origins[person['flow_id']].get(motion.edge_id, ())
            margin = self.flows.tolerance
            if (pid not in self.flows.dwelling and pid not in self.flows.retention_pending
                    and motion.stage_type == tc.STAGE_WALKING
                    and any(start - margin <= motion.lane_position <= end + margin for start, end in spans)):
                eligible.add(pid)
        return eligible

    def _edge(self, edge):
        if not isinstance(edge, str) or edge not in self.edges:
            raise ValueError('动作路段不在本次第二类可步行路网中')

    def _route_policy(self, policy):
        if set(policy) - {'avoidEdgeIds', 'riskWeights', 'keepRoute'}:
            raise ValueError('路线参数包含不支持的字段')
        edges = policy.get('avoidEdgeIds', [])
        weights = policy.get('riskWeights', {})
        keep = policy.get('keepRoute', False)
        if not isinstance(edges, list) or not isinstance(weights, dict) or type(keep) is not bool:
            raise ValueError('路线避让或权重格式无效')
        for edge in [*edges, *weights]:
            self._edge(edge)
        for weight in weights.values():
            number(weight, .1, 10, '风险权重')
        if keep == bool(edges or weights):
            raise ValueError('请选择保留路线，或提供避让路段/风险权重')

    def _validate_action(self, runtime, action, origins):
        if not isinstance(action, dict) or set(action) - {'actionId', 'actionType', 'target', 'parameters', 'startSeconds', 'endSeconds'}:
            raise ValueError('第二类动作格式无效')
        kind = action.get('actionType')
        identity = action.get('actionId')
        if not isinstance(kind, str) or kind not in ACTIONS or not isinstance(identity, str) or not IDENTIFIER.fullmatch(identity):
            raise ValueError('第二类动作类型或标识无效')
        target, params = action.get('target', {}), action.get('parameters', {})
        if not isinstance(target, dict) or not isinstance(params, dict):
            raise ValueError('动作目标与参数须为对象')
        flow = target.get('flowId')
        if ('flowId' in target and (not isinstance(flow, str) or flow not in self.members)):
            raise ValueError('动作人流不属于本次运行')
        start = self._time(runtime, action.get('startSeconds'))
        if start >= runtime.timeline_end_seconds:
            raise ValueError('动作开始时间已超出推演窗口')
        end = action.get('endSeconds')
        if end is not None or kind in INTERVAL_ACTIONS:
            if self._time(runtime, end) <= start:
                raise ValueError('动作结束时间须晚于开始时间')
        allowed_target = {'flowId'}
        if kind == 'set_inflow_rate':
            allowed_target |= {'entryId', 'direction'}
            entry = target.get('entryId')
            if not isinstance(entry, str) or entry not in GATES or target.get('direction') not in ('inbound', 'outbound'):
                raise ValueError('请选择本场景入口及通过方向')
            self._fields(params, {'rate'})
            number(params.get('rate'), 0, 1, '放行比例')
        elif kind == 'set_edge_capacity':
            allowed_target.add('edgeId')
            self._edge(target.get('edgeId'))
            self._fields(params, {'multiplier'})
            number(params.get('multiplier'), .1, 1, '行走限速倍率')
        elif kind == 'release_flow':
            self._fields(params, {'batchSize', 'intervalSeconds'})
            size, interval = params.get('batchSize'), params.get('intervalSeconds')
            if flow is None or type(size) is not int or not 1 <= size <= len(self.members[flow]):
                raise ValueError('分批起步须指定人流和有效批次人数')
            count = len(origins.intersection(self.members[flow]))
            if not count:
                raise ValueError('该人流已无可在起点暂缓的行人')
            interval = number(interval, runtime.step_length, runtime.timeline_end_seconds, '放行间隔')
            self._aligned(runtime, interval)
            if count and start + ((count - 1) // size) * interval >= (end or runtime.timeline_end_seconds):
                raise ValueError('最后一批须在动作窗口内放行')
        elif kind == 'reroute_group':
            if flow is None or params.get('keepRoute'):
                raise ValueError('改道须指定人流和改道条件')
            self._route_policy(params)
        elif kind == 'set_route_distribution':
            self._fields(params, {'distributions'})
            choices = params.get('distributions')
            if flow is None or not isinstance(choices, list) or not 2 <= len(choices) <= 4:
                raise ValueError('比例分流须指定人流和 2～4 个路线安排')
            ratios = []
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ValueError('分流路线格式无效')
                ratios.append(number(choice.get('ratio'), 0, 1, '分流比例'))
                self._route_policy({key: value for key, value in choice.items() if key != 'ratio'})
            if not isclose(sum(ratios), 1, abs_tol=1e-6) or sum(r > 0 for r in ratios) < 2:
                raise ValueError('至少两条路线的分流比例须大于零，合计为 1')
        elif kind == 'publish_guidance':
            allowed_target.add('edgeId')
            if 'edgeId' in target:
                self._edge(target['edgeId'])
            self._fields(params, {'message'})
            message = params.get('message')
            if not isinstance(message, str) or not 1 <= len(message.strip()) <= 500:
                raise ValueError('引导内容须为 1～500 字')
        else:
            allowed_target.add('edgeId')
            if 'edgeId' in target:
                self._edge(target['edgeId'])
            self._fields(params, set())
        self._fields(target, allowed_target)

    @staticmethod
    def _fields(value, allowed):
        if set(value) - allowed:
            raise ValueError('动作包含不支持的目标或参数')

    @staticmethod
    def _aligned(runtime, value):
        ticks = value / runtime.step_length
        if not isclose(ticks, round(ticks), rel_tol=0, abs_tol=EPSILON):
            raise ValueError('动作时间须与仿真步长对齐')

    def _time(self, runtime, value):
        seconds = number(value, runtime.time_seconds, runtime.timeline_end_seconds, '动作时间')
        self._aligned(runtime, seconds - runtime.time_seconds)
        return seconds

    def validate_plan(self, runtime, plan):
        if runtime.state.value not in {'READY', 'PAUSED'} or runtime.time_seconds >= runtime.timeline_end_seconds:
            raise ValueError('请在已初始化或暂停的仿真中装载策略')
        if not isinstance(plan, dict) or set(plan) != {'planId', 'source', 'actions'} or plan['source'] != self.binding(runtime):
            raise ValueError('策略对应的运行或采样帧已变化，请重新采样')
        plan_id, actions = plan['planId'], plan['actions']
        if not isinstance(plan_id, str) or not IDENTIFIER.fullmatch(plan_id) or not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
            raise ValueError(f'策略须有独立标识及 1～{MAX_ACTIONS} 条动作')
        origins, ids, releasing = self.origin_people(runtime), set(), set()
        for action in actions:
            self._validate_action(runtime, action, origins)
            if action['actionId'] in ids:
                raise ValueError('策略动作标识不能重复')
            ids.add(action['actionId'])
            if action['actionType'] == 'release_flow':
                flow = action['target']['flowId']
                if flow in releasing:
                    raise ValueError('同一人流只允许一份起步计划')
                releasing.add(flow)
        return origins

    def preview(self, runtime, plan):
        if isinstance(plan, dict) and set(plan) == {'planId', 'source', 'actionLines'}:
            plan = {**plan, 'actions': parse_action_lines(plan['actionLines'], self.flow_aliases)}
            del plan['actionLines']
        self.validate_plan(runtime, plan)
        return plan['actions']

    def install(self, runtime, plan):
        if self.plan is not None and plan == self.plan:
            return self.status()
        origins = self.validate_plan(runtime, plan)
        self.finish(runtime, cancelled=True)
        self.plan = deepcopy(plan)
        self.rows = {}
        for action in self.plan['actions']:
            row = {'action': action, 'status': 'scheduled', 'held': set(), 'seen': set(),
                   'speedErrors': set(), 'confirmed': set(), 'detail': {}}
            self.rows[action['actionId']] = row
            if action['actionType'] == 'release_flow':
                people = [p for p in self.members[action['target']['flowId']] if p in origins]
                row['seen'].update(people)
                self._hold(runtime, row, set(people))
                row.update(waiting=[pid for pid in people if pid in row['held']], nextRelease=action['startSeconds'],
                           releaseRetry=set(), releaseAbandoned=set())
                if not row['held']:
                    self._record(runtime, row, 'failed' if people else 'no_match')
        return self.status()

    def _hold(self, runtime, row, people, *, changes=None):
        action_id = row['action']['actionId']
        key = f"class2:{self.plan['planId']}:{action_id}"
        requested = row['held'] ^ people
        if changes is not None:
            requested &= changes
        failed = set()
        for pid in sorted(requested):
            held = pid in people
            motion = runtime.current.persons[pid]
            runtime.plan_executor.set_inflow_hold(pid, key, held)
            if self.flows.write_speed(runtime, motion, self.flows.speed_for(runtime, motion), operation=key):
                if held:
                    row['held'].add(pid)
                else:
                    row['held'].remove(pid)
                row['speedErrors'].discard(pid)
                row['confirmed'].add(pid)
            else:
                runtime.plan_executor.set_inflow_hold(pid, key, not held)
                failed.add(pid)
                row['speedErrors'].add(pid)
        return failed

    def status(self):
        if self.plan is None:
            return None
        return deepcopy({'planId': self.plan['planId'], 'source': self.plan['source'],
                         'actions': [self._receipt(row) for row in self.rows.values()]})

    @staticmethod
    def _receipt(row):
        receipt = {**row['action'], 'status': row['status'], 'matchedAgents': len(row['seen']),
                   'heldAgents': len(row['held']), **row['detail']}
        if row['action']['actionType'] == 'set_edge_capacity':
            receipt['appliedAgents'] = len(row['confirmed'])
        if row['speedErrors']:
            receipt['failedAgents'] = len(row['speedErrors'])
        return receipt

    def _record(self, runtime, row, status):
        row['status'] = status
        if runtime.recorder is not None:
            runtime.recorder.record_command({'action': 'class2_control', 'planId': self.plan['planId']},
                {'run_id': runtime.run_id, 'timeSeconds': runtime.time_seconds, **self._receipt(row)})

    def advance(self, runtime):
        now = runtime.time_seconds
        for row in self.rows.values():
            action = row['action']
            if row['status'] not in {'scheduled', 'active'}:
                continue
            end = action.get('endSeconds')
            if end is not None and now + EPSILON >= end:
                self._complete(runtime, row, allow_retry=True)
            elif now + EPSILON >= action['startSeconds']:
                self._execute(runtime, row)

    def _complete(self, runtime, row, *, allow_retry=False):
        if allow_retry and row['action']['actionType'] == 'set_edge_capacity' and row['status'] == 'active':
            row.setdefault('restorePending', row['confirmed'].copy())
            return
        row['speedErrors'].update(row.get('restorePending', ()))
        unprocessed = set(row.get('waiting', ())) - row['releaseAbandoned'] if 'waiting' in row else set()
        previous_failures = row['speedErrors'].copy()
        self._hold(runtime, row, set())
        # Clearing a hold outside its action window is cleanup, not a scheduled release.
        row['speedErrors'].update(previous_failures | unprocessed)
        if row['held'] and allow_retry and not row.get('closing'):
            row['closing'] = True
            self._record(runtime, row, 'active')
            return
        message_id = row['detail'].get('messageId')
        if message_id:
            row['detail']['deliveredAgents'] = sum(record.message_id == message_id for record in runtime.information.records)
        status = 'expired' if row['status'] == 'scheduled' else 'completed' if row['seen'] else 'no_match'
        self._record(runtime, row, self._status_with_speed_errors(row, status))

    @staticmethod
    def _status_with_speed_errors(row, status):
        if not row['speedErrors']:
            return status
        if row['action']['actionType'] == 'release_flow':
            succeeded = row['detail'].get('releasedAgents', 0)
        else:
            succeeded = row['confirmed']
        return 'partial' if succeeded else 'failed'

    def finish(self, runtime, *, cancelled=False):
        for row in self.rows.values():
            if row['status'] not in {'scheduled', 'active'}:
                if row['held']:
                    self._complete(runtime, row)
                continue
            if cancelled:
                self._hold(runtime, row, set())
                message_id = row['detail'].get('messageId')
                if message_id:
                    runtime.information.messages.pop(message_id, None)
                    row['detail']['deliveredAgents'] = sum(record.message_id == message_id for record in runtime.information.records)
                self._record(runtime, row, self._status_with_speed_errors(row, 'cancelled'))
            else:
                self._complete(runtime, row)
        if cancelled and any(row['held'] for row in self.rows.values()):
            raise ValueError('旧策略仍有未解除的等待，未装载新策略，可重试')

    def _matches(self, runtime, target):
        flow, edge = target.get('flowId'), target.get('edgeId')
        return {pid for pid, motion in runtime.current.persons.items()
                if (flow is None or self.flows.people[pid]['flow_id'] == flow)
                and (edge is None or motion.edge_id == edge)}

    def _execute(self, runtime, row):
        action = row['action']
        kind = action['actionType']
        target, params = action.get('target', {}), action.get('parameters', {})
        if kind == 'release_flow':
            self._release_batch(runtime, row, params)
        elif kind == 'set_inflow_rate':
            self._gate(runtime, row, target, params['rate'])
        elif kind == 'set_edge_capacity':
            row['seen'].update(self._matches(runtime, target) - self.flows.at_or_approaching_retention)
            if row['status'] == 'scheduled':
                self._record(runtime, row, 'active')
        elif kind == 'publish_guidance':
            if row['status'] == 'scheduled':
                self._guidance(runtime, row, target, params['message'])
        elif kind == 'observe_only':
            row['seen'].update(self._matches(runtime, target))
            sample = runtime.latest_metrics.get('observation', {})
            row['detail']['observation'] = {key: sample[key] for key in ('snapshot_id', 'time_seconds', 'metric_ids', 'global') if key in sample}
            row['detail']['observation']['scope'] = 'scene'
            if row['seen'] and set(sample.get('metric_ids', ())) & {'global-speed', 'local-speed'}:
                row['detail']['targetMeanSpeedMps'] = sum(runtime.current.persons[pid].speed for pid in row['seen']) / len(row['seen'])
            self._record(runtime, row, 'completed')
        else:
            choices = params['distributions'] if kind == 'set_route_distribution' else [{**params, 'ratio': 1.0}]
            self._reroute(runtime, row, target, choices)

    def _release_batch(self, runtime, row, params):
        retry, abandoned = row['releaseRetry'], row['releaseAbandoned']
        batch = set(retry)
        if runtime.time_seconds + EPSILON >= row['nextRelease']:
            waiting = [pid for pid in row['waiting'] if pid not in retry and pid not in abandoned]
            batch.update(waiting[:params['batchSize'] - len(batch)])
            if batch - retry:
                row['nextRelease'] += params['intervalSeconds']
        if not batch:
            return
        failed = self._hold(runtime, row, row['held'] - batch)
        abandoned.update(failed & retry)
        row['releaseRetry'] = failed - retry
        row['waiting'] = [pid for pid in row['waiting'] if pid in row['held']]
        row['detail']['releasedAgents'] = row['detail'].get('releasedAgents', 0) + len(batch - failed)
        status = 'active' if any(pid not in abandoned for pid in row['waiting']) else self._status_with_speed_errors(row, 'completed')
        self._record(runtime, row, status)

    def _speed_actions(self, pid, edge):
        flow = self.flows.people[pid]['flow_id']
        for row in self.rows.values():
            action = row['action']
            if row['status'] != 'active' or action['actionType'] != 'set_edge_capacity' or 'restorePending' in row:
                continue
            target = action['target']
            if target['edgeId'] == edge and target.get('flowId', flow) == flow:
                yield row

    def speed_limit(self, pid, edge):
        speed = self.flows.speeds[pid]
        return min((speed * row['action']['parameters']['multiplier'] for row in self._speed_actions(pid, edge)), default=speed)

    def confirm_speed(self, pid, edge, actual, desired):
        for row in self._speed_actions(pid, edge):
            if pid not in row['seen']:
                continue
            if actual <= self.flows.speeds[pid] * row['action']['parameters']['multiplier']:
                row['confirmed'].add(pid)
                row['speedErrors'].discard(pid)
            else:
                row['speedErrors'].add(pid)
        if actual >= desired:
            for row in self.rows.values():
                if 'restorePending' in row:
                    row['restorePending'].discard(pid)

    def finish_speed_restoration(self, runtime):
        for row in self.rows.values():
            if row['status'] != 'active' or 'restorePending' not in row:
                continue
            if not row['restorePending'] or row.get('restoreAttempted'):
                self._complete(runtime, row)
            else:
                row['restoreAttempted'] = True

    def _gate(self, runtime, row, target, rate):
        entry = target['entryId']
        if 'meter' not in row:
            width = sum(lane.getWidth() for lane in runtime.network.edges[entry].getLanes() if lane.allows('pedestrian'))
            row['meter'] = InflowMeter(row['action']['actionId'], entry, rate, max(.5, width * 1.3), runtime.time_seconds)
        candidates = (runtime._entry_meter_candidates(entry) & self._matches(runtime, target)) - self.flows.at_or_approaching_retention
        candidates = {pid for pid in candidates if (runtime.current.persons[pid].edge_id in RING_EDGES) == (target['direction'] == 'outbound')}
        held, _ = row['meter'].regulate(candidates, runtime.time_seconds)
        row['seen'].update(candidates)
        changes = row['held'] ^ held
        previous = row.get('gateRetries', {})
        attempts = {pid: previous.get((pid, pid in held), 0) for pid in changes}
        failed = self._hold(runtime, row, held, changes={pid for pid in changes if attempts[pid] < 2})
        row['gateRetries'] = {(pid, pid in held): min(2, attempts[pid] + 1)
                              for pid in changes if pid in failed or attempts[pid] >= 2}
        admitted = (candidates & row['meter'].admitted) - row['held']
        row.setdefault('released', set()).update(admitted)
        row['confirmed'].update(admitted)
        row['detail']['releasedAgents'] = len(row['released'])
        if row['status'] == 'scheduled':
            self._record(runtime, row, 'active')

    def _guidance(self, runtime, row, target, message):
        people = self._matches(runtime, target)
        row['seen'].update(people)
        if not people:
            self._record(runtime, row, 'no_match')
            return
        action = row['action']
        message_id = f"class2:{self.plan['planId']}:{action['actionId']}"
        runtime.information.publish(InformationMessage(message_id, message, 'official', runtime.time_seconds,
            runtime.time_seconds, action['endSeconds'], recipient_ids=tuple(sorted(people)), command_type='inform'))
        row['detail']['messageId'] = message_id
        self._record(runtime, row, 'active')

    def _reroute(self, runtime, row, target, choices):
        people = sorted(pid for pid in self._matches(runtime, target) - self.flows.at_or_approaching_retention
                        if runtime.current.persons[pid].stage_type == tc.STAGE_WALKING
                        and not runtime.current.persons[pid].edge_id.startswith(':'))
        row['seen'].update(people)
        wanted = [choice['ratio'] * len(people) for choice in choices]
        counts = [floor(count) for count in wanted]
        for i in sorted(range(len(counts)), key=lambda i: wanted[i] - counts[i], reverse=True)[:len(people) - sum(counts)]:
            counts[i] += 1
        offset, totals, outcomes = 0, Counter(), []
        for choice, count in zip(choices, counts):
            result = Counter(self._reroute_person(runtime, pid, choice) for pid in people[offset:offset + count])
            totals.update(result)
            outcomes.append({'assignedAgents': count, **result})
            offset += count
        row['detail'].update(affectedAgents=totals['rerouted'], keptAgents=totals['kept'],
                             unreachableAgents=totals['unreachable'], rejectedAgents=totals['rejected'],
                             failedAgents=totals['unreachable'] + totals['rejected'], routes=outcomes)
        if not people:
            status = 'no_match'
        elif row['detail']['failedAgents']:
            status = 'partial' if totals['rerouted'] or totals['kept'] else 'failed'
        else:
            status = 'completed'
        self._record(runtime, row, status)

    def _reroute_person(self, runtime, pid, choice):
        if choice.get('keepRoute'):
            return 'kept'
        motion, target = runtime.current.persons[pid], self.flows.people[pid]['destination']
        router = runtime.route_provider.position_router
        speed = self.flows.speeds[pid]
        costs = {edge: router.edge_lengths[edge] / speed * weight for edge, weight in choice.get('riskWeights', {}).items()}
        avoid = choice.get('avoidEdgeIds', [])
        try:
            router.resolve_position(motion.edge_id, motion.lane_position)
        except ValueError as exc:
            self.flows.record_fault(runtime, motion, 'reroute_position', exc)
            return 'rejected'
        try:
            stage = runtime.adapter.current_person_stage(pid)
        except TraCIException as exc:
            self.flows.record_fault(runtime, motion, 'reroute_stage', exc)
            return 'rejected'
        if stage.type != tc.STAGE_WALKING:
            self.flows.record_fault(runtime, motion, 'reroute_stage', f'当前阶段 {stage.type} 不可改道')
            return 'rejected'
        try:
            route = router.route(EdgePosition(motion.edge_id, motion.lane_position), EdgePosition(target['edge'], target['position']),
                forbidden_edges=avoid, edge_costs=costs, default_speed_mps=speed)
            if any(edge in avoid for edge in route.edges):
                return 'unreachable'
            current = tuple(stage.edges)
            if motion.edge_id in current and route.edges == current[current.index(motion.edge_id):]:
                return 'kept'
        except PositionRouteUnavailable:
            return 'unreachable'
        result = runtime.apply_plan(BehaviorPlan(pid, runtime.snapshot_id, 'reroute', route_edges=route.edges,
            arrival_position=target['position'], reason='已确认的第二类改道', source='class2_control',
            decided_at=runtime.time_seconds, preserve_future_stages=True))
        if result.status == 'partial_failure':
            raise RuntimeError(f'行人 {pid} 改道后无法恢复，T+{runtime.time_seconds}s：{result.reason}')
        if result.status == 'rejected':
            self.flows.record_fault(runtime, motion, 'reroute_rejected', result.reason)
        return 'rerouted' if result.status == 'applied' else 'rejected'
