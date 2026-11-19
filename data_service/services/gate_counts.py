"""Whole-window, site-wide entrance counts used as a demand profile."""
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..prediction.inputs import default_settings, task_times


def count_gates(task):
    points = default_settings(task)['exit_points']
    return sorted(points) if points else ['A', 'B', 'C']


def from_counts(task, dataset, current_scope):
    if not isinstance(dataset, dict) or dataset.get('scope') != current_scope:
        raise ValueError('入口计数的研究范围、入口位置或时间窗已变化，请重新核对并导入')
    if not isinstance(dataset.get('source'), str) or not 0 < len(dataset['source'].strip()) <= 300:
        raise ValueError('请填写计数来源，最多 300 字')
    kinds = {'observed': '本时段计数', 'reference': '可比时段计数', 'scenario': '情景样例'}
    if not isinstance(dataset.get('kind'), str) or dataset['kind'] not in kinds:
        raise ValueError('请选择计数性质')
    try:
        measured = datetime.fromisoformat(dataset['observed_start'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('请填写计数起始时间（上海时间）') from None
    start, end, duration = task_times(task)
    if measured.tzinfo or (dataset['kind'] == 'observed' and measured != start):
        raise ValueError('本时段计数起点须与研究窗口一致，时间均采用上海本地时间')
    if dataset['kind'] != 'scenario' and measured + (end-start) > datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None):
        raise ValueError('实测与可比资料须已完成计数；未来时段请使用情景样例')
    if dataset['kind'] == 'reference' and measured + (end-start) > start:
        raise ValueError('可比时段的计数应来自研究开始前已结束的时段')
    gates = count_gates(task)
    times = range(0, math.ceil(duration), 5)
    rows = dataset.get('records')
    if not isinstance(rows, list) or len(rows) != len(gates)*len(times):
        raise ValueError('每个入口每五分钟须有一行，零人请填 0；资料须覆盖全时段')
    totals = {gate: 0 for gate in gates}
    bins = {minute: [0, 0] for minute in times}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'minute', 'gate', 'in', 'out'}:
            raise ValueError('计数行须包含开始分钟、入口、进入人数和离开人数')
        minute, gate = row['minute'], row['gate']
        if type(minute) is not int or minute not in times or not isinstance(gate, str) or gate not in totals:
            raise ValueError('开始分钟或入口编号与任务不符')
        if (minute, gate) in seen:
            raise ValueError(f'第 {minute} 分钟入口 {gate} 的记录重复')
        seen.add((minute, gate))
        if any(type(row[k]) is not int or not 0 <= row[k] <= 10000000 for k in ('in','out')):
            raise ValueError('进出人数须为非负整数，单项最多 1000 万')
        totals[gate] += row['in']
        bins[minute][0] += row['in']
        bins[minute][1] += row['out']
    series = []
    for minute, (entered, left) in bins.items():
        length = min(5, duration-minute)
        rate = entered / length
        if rate > 3000:
            raise ValueError('入口到达率超过当前实验支持的 3000 人/分')
        series.append(dict(minute=minute, minutes=length, visits=entered, departures=left, rate=rate,
                           start=(start+timedelta(minutes=minute)).isoformat(),
                           end=(start+timedelta(minutes=minute+length)).isoformat()))
    total = sum(totals.values())
    inputs = dict(arrival_schedule=[dict(minute=b['minute'],rate=b['rate']) for b in series],
                  arrival_interpolation='step', arrival_rate_per_minute=total/duration)
    if total:
        inputs['entrance_shares'] = [totals[g]/total for g in gates]
    provenance = dict(source=dataset['source'], kind=kinds[dataset['kind']], observed_start=measured.isoformat(),
                      entered=total, left=sum(b['departures'] for b in series), by_gate=totals,
                      interval_count=len(series))
    return dict(method='gate-counts-v1', status='ready', total_visits=total, bins=series,
                forecast_inputs=inputs, gate_counts=provenance, activity_count=0, activity_delta=0,
                baseline_visits=total, activity_decisions=[], scope=current_scope, relations=[],
                timing_note='各入口进入人数按时段汇总，活动到访已包含在计数中。')
