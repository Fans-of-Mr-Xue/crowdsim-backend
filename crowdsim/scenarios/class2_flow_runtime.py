"""Per-run class-two movement/retention; no changes to class-one activity rules."""
from __future__ import annotations

import json
from statistics import mean
from pathlib import Path

import traci.constants as tc
from traci._simulation import Stage
from traci.exceptions import TraCIException

from crowdsim.infrastructure.sumo_adapter import UNKNOWN_PERSON_STAGE
from .class2_control import Class2Control


class Class2FlowController:
    tolerance = .06
    margin = .025

    def __init__(self, layout_file):
        self.layout = json.loads(Path(layout_file).read_text())
        self.people = {p['person_id']: p for p in self.layout['positions']}
        self.speeds = {pid: p['speed'] for pid, p in self.people.items()}
        self.dwelling = set()
        self.retention_pending = {}
        self.unknown_stages = set()
        self.confirmed_speeds = {}
        self.speed_retries = {}
        self.control = Class2Control(self)
        self._map_geometry = None

    def map_geometry(self, runtime):
        if self._map_geometry is None:
            from .class2_map_geometry import build_map_geometry
            self._map_geometry = build_map_geometry(runtime, self.layout)
        return self._map_geometry

    @property
    def at_or_approaching_retention(self):
        return self.dwelling | self.retention_pending.keys()

    def bind(self, runtime):
        if set(runtime.current.persons) != set(self.people):
            raise RuntimeError('第二类初始化行人与冻结人流配置不一致')
        runtime.plan_executor.base_limits.update(self.speeds)
        self.update(runtime, runtime.current)
        for pid, motion in runtime.current.persons.items():
            limit = 0.0 if pid in self.unknown_stages else runtime.plan_executor.effective_speed(pid)
            self.write_speed(runtime, motion, limit, operation='initial_speed')
        return runtime.adapter.snapshot()

    def _mark_dwelling(self, runtime, pid, motion):
        if motion.stage_type != tc.STAGE_WALKING:
            # A coincident origin/destination can complete its first walk at
            # insertion. Restore a zero-speed walking stage so the person stays
            # in the pedestrian interaction model instead of a waiting stage.
            length = runtime.network.edges[motion.edge_id].getLength()
            guard = .001 if motion.lane_position > length/2 else max(.001, length-.001)
            runtime.adapter.replace_current_person_stage(pid, Stage(
                type=tc.STAGE_WALKING, edges=[motion.edge_id],
                departPos=motion.lane_position, arrivalPos=guard))
        runtime.plan_executor.hold_activity(pid, None)
        if self.write_speed(runtime, motion, 0.0, operation='destination_hold') or self.confirmed_speeds[pid] == 0:
            self.retention_pending.pop(pid, None)
            self.dwelling.add(pid)
            runtime.population.states[pid].activity_state = 'class2_dwelling'
        else:
            self.retention_pending.setdefault(pid, 0)

    def record_fault(self, runtime, motion, operation, reason):
        if runtime.recorder is not None:
            runtime.recorder.record_command(
                {'action': 'class2_person_fault', 'operation': operation},
                {'run_id': runtime.run_id, 'person_id': motion.person_id, 'timeSeconds': motion.time_seconds,
                 'edge': motion.edge_id, 'stage': motion.stage_type, 'reason': str(reason)},
            )

    def write_speed(self, runtime, motion, limit, *, operation):
        pid = motion.person_id
        try:
            runtime.adapter.set_person_speed(pid, limit)
        except TraCIException as exc:
            self.record_fault(runtime, motion, operation, exc)
            if pid not in self.confirmed_speeds:
                raise RuntimeError(f'无法确认行人 {pid} 的初始速度，T+{motion.time_seconds}s') from exc
            return False
        self.confirmed_speeds[pid] = limit
        self.speed_retries.pop(pid, None)
        return True

    def _queue_retention(self, runtime, motion, target):
        pid = motion.person_id
        attempts = self.retention_pending.get(pid, 0)
        if attempts >= 2:
            raise RuntimeError(f'行人 {pid} 的终点位置无法确认，T+{motion.time_seconds}s')
        self.retention_pending[pid] = attempts + 1
        runtime.plan_executor.hold_activity(pid, None)
        # SUMO applies this on its next step. Keep the guard walk's direction.
        try:
            runtime.adapter.move_person_on_route(pid, target['edge'], target['x'], target['y'], motion.angle)
        except TraCIException as exc:
            self.record_fault(runtime, motion, 'destination_position', exc)

    def update(self, runtime, step):
        if set(step.persons) != set(self.people):
            raise RuntimeError(f'第二类行人在到达后被移除或出现人数不守恒，T+{step.time_seconds}s')
        for pid, motion in step.persons.items():
            if motion.stage_type == UNKNOWN_PERSON_STAGE:
                if pid in self.unknown_stages:
                    raise RuntimeError(f'连续两帧无法确认行人 {pid} 的阶段，T+{step.time_seconds}s')
                self.unknown_stages.add(pid)
                self.record_fault(runtime, motion, 'read_person_stage', '阶段暂不可读，将暂停该行人一帧')
                continue
            self.unknown_stages.discard(pid)
            target = self.people[pid]['destination']
            at_target = motion.edge_id == target['edge'] and abs(motion.lane_position-target['position']) <= self.tolerance
            # The generated itinerary is destination walk, retention walk, then stop.
            # SUMO may consume the destination walk between two position samples.
            retention_walk = motion.stage_type == tc.STAGE_WALKING and motion.remaining_stage_count == 2
            if motion.stage_type != tc.STAGE_WALKING and (pid in self.dwelling or not at_target):
                raise RuntimeError(f'第二类行人 {pid} 的停留保留行程异常结束，T+{step.time_seconds}s')
            if retention_walk and not at_target:
                self._queue_retention(runtime, motion, target)
            elif at_target and (pid not in self.dwelling or pid in self.retention_pending):
                self._mark_dwelling(runtime, pid, motion)

    def speed_for(self, runtime, motion):
        pid = motion.person_id
        if pid in self.unknown_stages:
            return 0.0
        limit = min(runtime.plan_executor.effective_speed(pid), self.control.speed_limit(pid, motion.edge_id))
        target = self.people[pid]['destination']
        if pid not in self.dwelling and motion.edge_id == target['edge']:
            distance = abs(motion.lane_position - target['position'])
            limit = min(limit, max(0, (distance-self.margin)/runtime.step_length))
        return limit

    def before_step(self, runtime):
        for pid, motion in runtime.current.persons.items():
            limit = self.speed_for(runtime, motion)
            previous_limit, attempts = self.speed_retries.get(pid, (None, 0))
            if previous_limit != limit:
                attempts = 0
            if attempts >= 2 or not self.write_speed(runtime, motion, limit, operation='step_speed'):
                self.speed_retries[pid] = (limit, min(2, attempts + 1))
            actual = self.confirmed_speeds[pid]
            if actual != 0 and (pid in self.unknown_stages or pid in self.retention_pending or pid in self.dwelling):
                raise RuntimeError(f'无法确认行人 {pid} 已停止，T+{motion.time_seconds}s')
            self.control.confirm_speed(pid, motion.edge_id, actual, limit)
        self.control.finish_speed_restoration(runtime)

    def metrics(self, runtime=None):
        flows = []
        for f in self.layout['flow_design']['flows']:
            ids = {pid for pid, p in self.people.items() if p['flow_id'] == f['id']}
            flows.append({'id': f['id'], 'name': f['name'], 'count': f['count'],
                          'dwelling': len(ids & self.dwelling), 'walking': len(ids - self.dwelling),
                          'configured_speed_mps': f['speed']})
        result = {'total': len(self.people), 'dwelling': len(self.dwelling),
                'walking': len(self.people)-len(self.dwelling), 'flows': flows,
                'flow_design_sha256': self.layout['flow_design_sha256']}
        if runtime is not None:
            result['evaluation'] = capture_flow_state(runtime, self)
        return result


def capture_flow_state(runtime, flows):
    """Class-two-only measurement; no commands, navigation or person state mutations."""
    planned = set(runtime.plan_executor.flow_hold_reasons) | set(runtime.plan_executor.activity_hold_until)
    arrived = flows.dwelling
    pending = set(flows.retention_pending)
    threshold = runtime.visual_policy.low_speed_threshold_mps
    duration = runtime.visual_policy.confirmation_duration_seconds
    rows = []
    for flow in flows.layout['flow_design']['flows']:
        ids = {pid for pid, p in flows.people.items() if p['flow_id'] == flow['id']}
        travelling = [runtime.current.persons[pid] for pid in ids-arrived-pending-planned if pid in runtime.current.persons]
        rows.append({'id': flow['id'], 'name': flow['name'], 'total': flow['count'], 'arrived': len(ids & arrived),
                     'plannedWaiting': len(ids & (planned-arrived-pending)),
                     'slowWaiting': sum(m.speed < threshold and runtime.population.states[m.person_id].low_speed_duration >= duration for m in travelling),
                     'meanTravelSpeedMps': mean(m.speed for m in travelling) if travelling else None,
                     'travelCount': len(travelling)})
    count = sum(r['travelCount'] for r in rows)
    return {'flows': rows, 'total': sum(r['total'] for r in rows), 'arrived': sum(r['arrived'] for r in rows),
            'plannedWaiting': sum(r['plannedWaiting'] for r in rows), 'slowWaiting': sum(r['slowWaiting'] for r in rows),
            'meanTravelSpeedMps': sum((r['meanTravelSpeedMps'] or 0)*r['travelCount'] for r in rows)/count if count else None,
            'lowSpeedThresholdMps': threshold, 'lowSpeedDurationSeconds': duration}
