"""Bounded, configuration-scoped baseline preview; never advance the live run."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BIN_SECONDS = 10
MAX_ROADS = 8
TIMEOUT_SECONDS = 180


def _interval(rows, now, step):
    if rows and now - rows[-1][1] <= step + 1e-6:
        rows[-1][1] = now
    else:
        rows.append([now, now])


class BaselineCollector:
    """Observe native steps; ten-second bins only compress the model input."""

    def __init__(self, runtime, reference_edges=()):
        self.start = runtime.time_seconds
        self.end = runtime.timeline_end_seconds
        self.step = runtime.step_length
        self.flow_order = [f['id'] for f in runtime.class2_flows.layout['flows']]
        self.flow_indices = {fid: i for i, fid in enumerate(self.flow_order)}
        self.reference_edges = set(reference_edges)
        self.roads = {}
        self.samples = 0
        self.bin_samples = Counter()
        self.conserved = True
        self.threshold = runtime.visual_policy.low_speed_threshold_mps
        self.duration = runtime.visual_policy.confirmation_duration_seconds

    def sample(self, runtime):
        now, flows = runtime.time_seconds, runtime.class2_flows
        current = runtime.current.persons
        self.conserved = self.conserved and len(current) == len(flows.people)
        key = min(int((now-self.start)//BIN_SECONDS), max(0, math.ceil((self.end-self.start)/BIN_SECONDS)-1))
        self.bin_samples[key] += 1
        by_road = defaultdict(list)
        for pid, motion in current.items():
            if not motion.edge_id.startswith(':'):
                by_road[motion.edge_id].append((pid, motion))
        for edge, people in by_road.items():
            road = self.roads.setdefault(edge, {'bins': {}, 'shared': {}, 'near': {}, 'peak': 0, 'waiting': 0})
            counts = Counter(flows.people[pid]['flow_id'] for pid, _ in people)
            road['peak'] = max(road['peak'], len(people))
            active = [(pid, motion) for pid, motion in people if pid not in flows.dwelling
                      and pid not in flows.retention_pending]
            waiting = sum(runtime.state.value != 'READY' and motion.speed < self.threshold
                and runtime.population.states[pid].low_speed_duration >= self.duration
                and pid not in runtime.plan_executor.flow_hold_reasons
                and pid not in runtime.plan_executor.activity_hold_until for pid, motion in active)
            road['waiting'] = max(road['waiting'], waiting)
            bucket = road['bins'].setdefault(key, {'counts': Counter(), 'peak': 0,
                'speedSum': 0.0, 'speedSamples': 0, 'waiting': 0, 'dwelling': 0})
            bucket['counts'].update(counts)
            bucket['peak'] = max(bucket['peak'], len(people))
            if runtime.state.value != 'READY':
                bucket['speedSum'] += sum(m.speed for _, m in active)
                bucket['speedSamples'] += len(active)
            bucket['waiting'] = max(bucket['waiting'], waiting)
            bucket['dwelling'] = max(bucket['dwelling'], len(people)-len(active))
            ids = sorted(counts, key=self.flow_indices.get)
            for i, left in enumerate(ids):
                for right in ids[i+1:]:
                    pair = (self.flow_indices[left], self.flow_indices[right])
                    _interval(road['shared'].setdefault(pair, []), now, self.step)
            # Spatial hash: contact is a sampled proximity reference, not a risk threshold.
            grid = defaultdict(list)
            contacts = set()
            for pid, motion in active:
                cell = math.floor(motion.x/2), math.floor(motion.y/2)
                fi = self.flow_indices[flows.people[pid]['flow_id']]
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for other_fi, other in grid[(cell[0]+dx, cell[1]+dy)]:
                            if fi != other_fi and (motion.x-other.x)**2+(motion.y-other.y)**2 <= 4:
                                contacts.add(tuple(sorted((fi, other_fi))))
                grid[cell].append((fi, motion))
            for pair in contacts:
                _interval(road['near'].setdefault(pair, []), now, self.step)
        self.samples += 1

    def result(self, runtime):
        # Include empty samples in mean occupancy, rather than inflating sparse bins.
        roads = []
        ranked = sorted(self.roads.items(), key=lambda item: (
            item[0] not in self.reference_edges, -item[1]['waiting'], not bool(item[1]['near']),
            not bool(item[1]['shared']), -item[1]['peak'], item[0]))
        for edge, road in ranked[:MAX_ROADS]:
            rows = []
            for key, bucket in sorted(road['bins'].items()):
                start = self.start + key*BIN_SECONDS
                end = min(self.end, start+BIN_SECONDS)
                count = self.bin_samples[key]
                rows.append([start, end, [round(bucket['counts'][fid]/count, 1) for fid in self.flow_order],
                    bucket['peak'], round(bucket['speedSum']/bucket['speedSamples'], 3) if bucket['speedSamples'] else None,
                    bucket['waiting'], bucket['dwelling']])
            pack = lambda entries: [{'flows': list(pair), 'intervals': spans} for pair, spans in sorted(entries.items())]
            roads.append({'edgeId': edge, 'peakPeople': road['peak'], 'slowWaitingPeak': road['waiting'],
                'sharedRoadIntervals': pack(road['shared']), 'nearCrossFlowIntervals': pack(road['near']), 'bins': rows})
        return {'status': 'available', 'method': 'same_engine_uncontrolled_baseline',
            'sourceTimeSeconds': self.start, 'endSeconds': self.end, 'stepSeconds': self.step,
            'binSeconds': BIN_SECONDS, 'flowOrder': self.flow_order, 'reportedRoads': len(roads), 'totalRoads': len(self.roads),
            'binFields': ['startSeconds', 'endSeconds', 'meanPeopleByFlow', 'peakPeople', 'meanTravelSpeedMps', 'slowWaitingPeak', 'dwellingPeak'],
            'waitingThresholds': {'speedBelowMps': self.threshold, 'durationAtLeastSeconds': self.duration},
            'semantics': '初始化同配置、同种子、无管理决策的独立SUMO预演；不是施策后效果或现实人群保证。flows为flowOrder的下标。sharedRoadIntervals仅表示同段在场；nearCrossFlowIntervals表示同段跨人流距离≤2米的采样，不等同于对冲或风险阈值。slowWaitingPeak复用已有低速持续计时及waitingThresholds，排除当前管理暂缓和到达停留，仅作排队参考。均速剔除已到达停留者，READY静止帧不参与均速或滞留判断。',
            'roads': roads, 'finalDwelling': len(runtime.class2_flows.dwelling), 'sampleCount': self.samples,
            'populationConserved': self.conserved}


class Class2ForecastService:
    """One short-lived child at a time, one immutable configuration in memory."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.cached_key = None
        self.cached = None
        self.process = None

    async def close(self):
        proc = self.process
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

    async def forecast(self, runtime):
        record = deepcopy(runtime.requirement_record)
        key = (record['fingerprint'], runtime.random_seed, runtime.adapter.sumo_binary)
        async with self.lock:
            if key == self.cached_key:
                return deepcopy(self.cached)
            with tempfile.TemporaryDirectory(prefix='crowdsim-class2-forecast-') as directory:
                folder = Path(directory)
                (folder/'request.json').write_text(json.dumps({'requirement': record,
                    'sumoBinary': runtime.adapter.sumo_binary, 'seed': runtime.random_seed}, ensure_ascii=False))
                try:
                    self.process = await asyncio.create_subprocess_exec(sys.executable, '-B', '-m',
                        'crowdsim.scenarios.class2_forecast_worker', str(folder), cwd=ROOT,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
                    _, error = await asyncio.wait_for(self.process.communicate(), TIMEOUT_SECONDS)
                    if self.process.returncode != 0:
                        raise ValueError('基线预演失败：'+error.decode(errors='replace')[-400:])
                    result = json.loads((folder/'result.json').read_text())
                    if result.get('status') != 'available' or not result.get('populationConserved'):
                        raise ValueError('基线预演未完整完成或人数不守恒')
                    self.cached_key, self.cached = key, result
                    return deepcopy(result)
                except asyncio.TimeoutError:
                    return {'status': 'unavailable', 'reason': '基线预演超时；仅可使用当前观测及自由通行共路参考。'}
                except (ValueError, OSError) as exc:
                    return {'status': 'unavailable', 'reason': str(exc)}
                finally:
                    await self.close()
                    self.process = None
