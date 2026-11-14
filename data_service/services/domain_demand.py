"""Explainable scenario demand, independent of the downstream crowd solvers.

Historical regional visits set a scale; matched transport counts adjust that
scale and its time distribution. Weather multipliers are explicit assumptions,
not a fitted temperature/rainfall model. No source is counted as extra people.
"""
import copy
import hashlib
import json
import math
from datetime import datetime, timedelta
from urllib.parse import urlparse

from .gate_counts import from_counts
from .activity_evidence import resolve_activity
from ..prediction.inputs import default_settings, task_times

VERSION = 'historical-transport-weather-v1'
OUTPUT_KEYS = ('arrival_rate_per_minute', 'arrival_schedule', 'arrival_interpolation')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:20]


def scope(task):
    start, end, _ = task_times(task)
    return {'start': start.isoformat(), 'end': end.isoformat(), 'timezone': 'Asia/Shanghai',
            'geometry_hash': digest(task['region']['geometry'])}


def initialize(task, portrait):
    history = portrait.get('visualizations', {}).get('history', {})
    if not history.get('records'):
        raise ValueError('请先提供与本场地相关的历史日累计客流记录')
    start, _, duration = task_times(task)
    # Deterministic illustrative feeder counts; neither live traffic nor a claim
    # about the historical stations. Replaced through the same source contract.
    reference = [600, 700, 800, 750, 650, 600, 500, 400, 250]
    simulated = [720, 850, 1040, 900, 780, 620, 450, 260, 140]
    transport, weather = [], []
    for minute in range(0, math.ceil(duration), 30):
        length = min(30, duration - minute)
        i = min(8, int(minute / duration * 9))
        interval = {'start': (start + timedelta(minutes=minute)).isoformat(),
                    'end': (start + timedelta(minutes=minute + length)).isoformat()}
        transport.append({**interval, 'passages': round(simulated[i] * length / 30, 3),
                          'reference_passages': round(reference[i] * length / 30, 3)})
        weather.append({**interval, 'temperature_c': 8 if i < 4 else 6, 'precipitation_mm': 0, 'factor': .9})
    return {'schema_version': 1, 'scope': scope(task), 'transport_use': 'context',
            'history': {'kind': 'historical', 'source': history['title'], 'scope': history['scope'],
                        'unit': 'visits/day', 'evidence_id': history['evidence_id'],
                        'records': [{'date': r['date'], 'visits': r['daily_visits']} for r in history['records']]},
            'transport': {'kind': 'simulated', 'source': '演示生成：周边接驳客流，不对应任何真实车站计数',
                          'scope': '周边接驳统计断面；演示中视为同口径、互不重复的一组计数', 'unit': 'passages/interval',
                          'reference_source': '同一统计范围的示例基准；非历史实测交通', 'records': transport},
            'weather': {'kind': 'scenario', 'source': '冷凉、无降水情景；不是目标日期的天气预报',
                        'scope': task['region'].get('name', '研究场地'), 'unit': 'celsius,mm/interval', 'records': weather,
                        'factor_note': '0.9 为到访意愿修正假设，尚未用本场地天气与客流记录标定；温度和降水仅描述情景。'},
            'assumptions': {'local_share': .015, 'window_share': .35,
                            'note': '演示暂设外滩日客流的 1.5% 到访本场地，其中 35% 在研究时段抵达；不是从历史报道测得，需局部计数或调查校准。'}}


def _number(value, low, high, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} 须在 {low}–{high} 之间')


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError(f'{label} 须为非空文本，最多 2000 字')


def _keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f'{label} 字段须为：' + ', '.join(keys))


def validate(task, value):
    keys = ['schema_version', 'scope', 'history', 'transport', 'weather', 'assumptions']
    _keys(value, keys + [k for k in ('transport_use', 'activities') if isinstance(value, dict) and k in value], '多源输入')
    if value.get('transport_use', 'counts') not in ('counts', 'context'):
        raise ValueError('交通用途须为 counts（客流计数换算）或 context（当前路况参考）')
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['scope'] != scope(task):
        raise ValueError('多源资料的时间窗或场地边界已变化，请按当前任务重新对齐资料')
    for key, modes, unit, extra in [('history', ('historical',), 'visits/day', ['evidence_id']),
                                   ('transport', ('simulated', 'observed'), 'passages/interval', ['reference_source']),
                                   ('weather', ('scenario', 'observed', 'forecast'), 'celsius,mm/interval', ['factor_note'])]:
        source = value[key]
        _keys(source, ['kind', 'source', 'scope', 'unit', 'records', *extra], key)
        if source['kind'] not in modes or source['unit'] != unit:
            raise ValueError(f'{key} 的数据性质或单位不符合接入约定')
        for field in ['source', 'scope', *extra]:
            _text(source[field], key + '.' + field)
        if not isinstance(source['records'], list) or not 1 <= len(source['records']) <= 200:
            raise ValueError(f'{key} 须包含 1–200 条记录')
    dates = set()
    task_start, task_end, _ = task_times(task)
    for row in value['history']['records']:
        _keys(row, ['date', 'visits'], '历史记录')
        date = datetime.strptime(row['date'], '%Y-%m-%d').date()
        if date in dates or date >= task_start.date():
            raise ValueError('历史日期须唯一且早于研究起始日')
        dates.add(date)
        _number(row['visits'], 1, 100000000, '历史日累计人次')
    intervals = []
    for key in ['transport', 'weather']:
        last, actual = task_start, []
        for row in value[key]['records']:
            fields = ['passages', 'reference_passages'] if key == 'transport' else ['temperature_c', 'precipitation_mm', 'factor']
            _keys(row, ['start', 'end', *fields], key + ' 记录')
            start, end = datetime.fromisoformat(row['start']), datetime.fromisoformat(row['end'])
            if start != last or not start < end <= task_end:
                raise ValueError('交通与天气记录须连续覆盖研究时间窗；不可缺段、重叠或越界，时间采用上海本地时间')
            if key == 'transport':
                _number(row['passages'], 0, 10000000, '交通断面人次')
                _number(row['reference_passages'], .001, 10000000, '同口径基准人次')
            else:
                _number(row['temperature_c'], -60, 60, '温度')
                _number(row['precipitation_mm'], 0, 300, '时段降水')
                _number(row['factor'], 0, 2, '天气到访系数')
            last = end
            actual.append((start, end))
        if last != task_end:
            raise ValueError('资料尚未覆盖完整研究时间窗')
        if intervals and intervals != actual:
            raise ValueError('交通与天气须使用相同的统计时段；请先对齐时间分段')
        intervals = actual
    a = value['assumptions']
    _keys(a, ['local_share', 'window_share', 'note'], '尺度换算假设')
    _number(a['local_share'], 0, 1, '场地到访比例')
    _number(a['window_share'], 0, 1, '研究时段比例')
    _text(a['note'], '换算假设依据')
    activities = value.get('activities', [])
    if not isinstance(activities, list) or len(activities) > 30:
        raise ValueError('活动设置最多 30 项')
    value = copy.deepcopy(value)
    activities = value.get('activities', [])
    used_ids, used_sources = set(), set()
    for activity in activities:
        _keys(activity, ['id','source_id','source_label','source_url','title','start','end','attendance','visit_share','additional_share','note','enabled'] + [k for k in ('assume_if_pending', 'timing') if k in activity], '活动设置')
        if 'assume_if_pending' in activity and type(activity['assume_if_pending']) is not bool:
            raise ValueError('待核对活动须明确是否作为情景假设')
        if 'timing' in activity:
            timing = activity['timing']
            _keys(timing, ['event_at', 'lead_minutes', 'lag_minutes'], '活动时间')
            _number(timing['lead_minutes'], 0, 1440, '提前抵达分钟')
            _number(timing['lag_minutes'], 0, 1440, '延后抵达分钟')
            event = datetime.fromisoformat(timing['event_at'])
            activity['start'] = (event - timedelta(minutes=timing['lead_minutes'])).isoformat()
            activity['end'] = (event + timedelta(minutes=timing['lag_minutes'])).isoformat()
        for field in ['id','source_id','source_label','title','note']:
            _text(activity[field], '活动 ' + field)
        url = activity['source_url']
        if not isinstance(url, str) or len(url) > 2000 or (url and (urlparse(url).scheme not in ('http', 'https') or not urlparse(url).netloc)):
            raise ValueError('活动来源链接须为空或有效的 HTTP / HTTPS 地址')
        if activity['id'] in used_ids or activity['source_id'] in used_sources:
            raise ValueError('同一活动来源只能设置一次，避免重复计数')
        used_ids.add(activity['id']); used_sources.add(activity['source_id'])
        if type(activity['enabled']) is not bool:
            raise ValueError('活动是否用于估计须为布尔值')
        if not isinstance(activity['start'], str) or not isinstance(activity['end'], str):
            raise ValueError('活动抵达时段须为上海本地 ISO 时间')
        begin, finish = datetime.fromisoformat(activity['start']), datetime.fromisoformat(activity['end'])
        if begin.tzinfo or finish.tzinfo:
            raise ValueError('活动抵达时段须为上海本地时间，无时区后缀')
        if not task_start <= begin < finish <= task_end:
            raise ValueError('活动的预计抵达时段须在完整研究时间窗内；历史活动请明确作为本次情景假设')
        _number(activity['attendance'], 0, 10000000, '活动参与人数')
        _number(activity['visit_share'], 0, 1, '到访本场地比例')
        _number(activity['additional_share'], 0, 1, '未含在基准中的新增比例')
    return value


def estimate(task, config):
    config = validate(task, config)
    start, end, duration = task_times(task)
    mean = sum(r['visits'] for r in config['history']['records']) / len(config['history']['records'])
    base = mean * config['assumptions']['local_share'] * config['assumptions']['window_share']
    rows, meteo = config['transport']['records'], config['weather']['records']
    context_only = config.get('transport_use') == 'context'
    reference = sum(r['reference_passages'] for r in rows)
    traffic = sum(r['passages'] for r in rows)
    transport_total = base if context_only else base * traffic / reference
    # Road congestion/vehicle speed is not a feeder pedestrian count. In context
    # mode, use a declared uniform arrival assumption until a calibrated temporal
    # demand profile is available; old mock counts do not influence any rate.
    raw_counts = [base * ((datetime.fromisoformat(r['end']) - datetime.fromisoformat(r['start'])).total_seconds() / 60 / duration
                         if context_only else r['passages'] / reference) * w['factor'] for r, w in zip(rows, meteo)]
    raw_total = sum(raw_counts)
    total = round(raw_total)
    bins, schedule = [], []
    # Round only the final demand count; retain precision in rates so integration
    # by ArrivalCurve exactly reproduces the displayed total over the full window.
    for row, weather, raw in zip(rows, meteo, raw_counts):
        begin, finish = datetime.fromisoformat(row['start']), datetime.fromisoformat(row['end'])
        length = (finish - begin).total_seconds() / 60
        visits = raw * total / raw_total if raw_total else 0
        rate = visits / length
        _number(rate, 0, 3000, '估计到达率（人次/分钟）')
        minute = (begin - start).total_seconds() / 60
        bins.append({**row, **{k: weather[k] for k in ['temperature_c', 'precipitation_mm', 'factor']},
                     'minute': minute, 'minutes': length, 'visits': visits, 'rate': rate})
        schedule.append({'minute': minute, 'rate': rate})
    outputs = {'arrival_rate_per_minute': total / duration, 'arrival_interpolation': 'step', 'arrival_schedule': schedule}
    method = 'historical-weather-road-context-v1' if context_only else VERSION
    result = {'method': method, 'fingerprint': digest({'method': method, 'inputs': config}), 'status': 'ready',
            'transport_use': 'context' if context_only else 'counts',
            'timing_note': '分时到达暂按研究时段均匀分配，再应用各时段天气系数；可在实验控制中调整到达曲线。' if context_only else '按同口径交通计数与天气系数构造分时到达。',
            'total_visits': total, 'unit': '人次 / 研究时段', 'historical_mean': mean, 'base_visits': base,
            'transport_total': transport_total, 'transport_factor': 1 if context_only else traffic / reference,
            'weather_factor': raw_total / transport_total if transport_total else 1,
            'transport_delta': transport_total - base, 'weather_delta': raw_total - transport_total,
            'bins': bins, 'forecast_inputs': outputs, 'scope': config['scope'],
            'limitation': '情景估计，尚未标定。总量为研究时段新增到达需求人次，不含起始在场人数，也不是去重人数、入场保证或场内峰值。'}

    return _activity_arrivals(task, config, result)


def _activity_arrivals(task, config, result):
    activities = config.get('activities', [])
    evidence = task.get('components', {}).get('domain_portrait', {}).get('evidence', [])
    decisions = [resolve_activity(row, evidence, config['scope']) for row in activities]
    enabled = [row for row, decision in zip(activities, decisions) if decision['included']]
    result['activity_decisions'] = decisions
    fingerprint_inputs = {'inputs': config}
    if any(row['notices'] for row in decisions):
        fingerprint_inputs['activity_decisions'] = decisions
        result['fingerprint'] = digest({'method': result['method'], **fingerprint_inputs})
    result['activity_delta'] = 0
    result['activity_count'] = len(enabled)
    result['baseline_visits'] = result['total_visits']
    if not enabled:
        return result
    start, _, duration = task_times(task)
    intervals = []
    for row in enabled:
        begin = (datetime.fromisoformat(row['start'])-start).total_seconds()/60
        end = (datetime.fromisoformat(row['end'])-start).total_seconds()/60
        visits = row['attendance'] * row['visit_share'] * row['additional_share']
        intervals.append((begin, end, visits/(end-begin)))
    times = sorted({0., duration, *[b['minute'] for b in result['bins']], *[t for lo,hi,_ in intervals for t in (lo,hi)]})
    baseline = result['bins']
    total_raw = result['total_visits'] + sum((hi-lo)*rate for lo,hi,rate in intervals)
    total = round(total_raw)
    scale = total/total_raw if total_raw else 0
    bins = []
    for lo, hi in zip(times, times[1:]):
        base = next(b for b in baseline if b['minute'] <= lo < b['minute']+b['minutes'])
        extra = sum(rate for begin,end,rate in intervals if begin <= lo < end)
        rate = (base['rate'] + extra) * scale
        _number(rate, 0, 3000, '活动叠加后的到达率（人次/分钟）')
        bins.append({**base, 'start':(start+timedelta(minutes=lo)).isoformat(), 'end':(start+timedelta(minutes=hi)).isoformat(),
                     'minute':lo, 'minutes':hi-lo, 'visits':rate*(hi-lo), 'rate':rate, 'activity_visits':extra*(hi-lo)*scale})
    method = result['method'] + '+activity-arrivals-v1'
    return {**result, 'method':method, 'fingerprint':digest({'method':method, **fingerprint_inputs}),
            'bins':bins, 'total_visits':total, 'activity_delta':total-result['baseline_visits'],
            'forecast_inputs':{'arrival_rate_per_minute':total/duration, 'arrival_interpolation':'step',
                               'arrival_schedule':[{'minute':b['minute'],'rate':b['rate']} for b in bins]},
            'timing_note':result['timing_note'] + ' 活动新增到访在设置的抵达时段内均匀分配，按用户填写的新增比例处理与基准的重叠。'}


def _previous_estimate(task, config):
    """Read-only preview of saved assumptions; never rebind or export to a solver."""
    saved_scope = config['scope']
    preview_task = copy.deepcopy(task)
    preview_task['time_window'] = f"{saved_scope['start']} 至 {saved_scope['end']}"
    preview_config = copy.deepcopy(config)
    # Geometry is not an operand of the demand formula. Validate the original
    # intervals and sources in a temporary scope without changing persisted data.
    preview_config['scope'] = {**saved_scope, 'geometry_hash': scope(preview_task)['geometry_hash']}
    result = estimate(preview_task, preview_config)
    from .domain_relations import occupancy_estimate
    settings = default_settings(preview_task)
    result['occupancy'] = occupancy_estimate(preview_task, result, settings)
    result.update(status='previous', scope=copy.deepcopy(saved_scope), applied=False,
                  initial_crowd=settings['initial_crowd'])
    result.pop('forecast_inputs')
    result.pop('fingerprint')
    return result


def gate_scope(task):
    return {**scope(task), 'exit_points': default_settings(task)['exit_points']}


def effective_estimate(task, portrait):
    if portrait.get('gate_counts') is not None:
        dataset = portrait['gate_counts']
        result = from_counts(task, dataset, gate_scope(task))
        result['fingerprint'] = digest({'method': result['method'], 'inputs': dataset})
        return result
    return estimate(task, portrait['demand_inputs'])


def project(task, portrait):
    config = portrait.get('demand_inputs')
    if not config and portrait.get('gate_counts') is None:
        return {'status': 'empty', 'can_initialize': bool(portrait.get('visualizations', {}).get('history', {}).get('records'))}
    try:
        result = effective_estimate(task, portrait)
    except (ValueError, TypeError, KeyError) as exc:
        try:
            current_scope = scope(task)
        except (ValueError, TypeError, KeyError):
            current_scope = None
        stale = {'status': 'stale', 'message': str(exc), 'current_scope': current_scope}
        if current_scope and config and portrait.get('gate_counts') is None and config.get('scope') != current_scope:
            try:
                stale['previous_estimate'] = _previous_estimate(task, config)
            except (ValueError, TypeError, KeyError):
                pass  # Invalid source data must not be disguised as a valid preview.
        return stale
    settings = default_settings(task)
    applied = portrait.get('demand_application', {})
    result['applied'] = applied.get('fingerprint') == result['fingerprint'] and all(settings.get(k) == v for k, v in result['forecast_inputs'].items())
    result['applied_task_revision'] = applied.get('task_revision') if result['applied'] else None
    result['initial_crowd'] = settings['initial_crowd']
    if result.get('gate_counts'):
        return result
    result['relations'] = [
        {'id': 'history', 'from': '历史客流', 'to': '到访规模', 'label': '确定规模参考', 'status': '历史资料 + 换算假设',
         'evidence_ids': [config['history']['evidence_id']], 'point_ids': [],
         'evidence': f"{len(config['history']['records'])} 天日累计记录；范围：{config['history']['scope']}。",
         'assumption': config['assumptions']['note'], 'output': '场地、时段换算后的基准到访人次；不直接当作场内人数。'},
        {'id': 'transport', 'from': '周边交通', 'to': '到达节奏', 'label': '修正规模与时序', 'status': '模拟输入' if config['transport']['kind'] == 'simulated' else '接入计数 · 口径待核',
         'evidence_ids': [], 'point_ids': ['north_access', 'south_access'], 'evidence': config['transport']['source'],
         'assumption': '与基准使用同范围、同方式、同时间长度的去重统计断面；比例变化暂视为场地到访变化。不把交通人数叠加到历史人数上。',
         'output': '分时到达率（人次/分钟）；断面计数仍需到访转化调查。'},
        {'id': 'weather', 'from': '天气条件', 'to': '参与意愿', 'label': '修正到访意愿', 'status': {'scenario': '天气情景', 'observed': '接入观测', 'forecast': '接入预报'}[config['weather']['kind']],
         'evidence_ids': [], 'point_ids': ['viewing'], 'evidence': config['weather']['source'],
         'assumption': config['weather']['factor_note'], 'output': '乘入分时到达率；当前不据此改变步速或路面条件。'},
        {'id': 'access', 'from': '场地通道', 'to': '人群进入', 'label': '分配接入位置', 'status': '路网坐标 + 比例假设',
         'evidence_ids': ['roads'], 'point_ids': ['north_access', 'south_access'],
         'evidence': '采用当前任务绑定的步行路网和接入坐标；地图可悬浮、点击核对道路。',
         'assumption': '通道编号与分配比例为建模设定，不代表已核验的官方出入口或观测分流比例。',
         'output': '场地接入坐标与来流分配比例：' + ' / '.join(f'{x * 100:g}%' for x in settings['entrance_shares'])},
        {'id': 'activity', 'from': '观景、摄影', 'to': '人群停留', 'label': '关联活动与位置', 'status': '活动依据 + 停留假设',
         'evidence_ids': ['official_monument', 'photo_guide'], 'point_ids': ['viewing'],
         'evidence': '场所介绍与摄影推荐支持观景、摄影这一活动类型；观景位置对应当前场地。',
         'assumption': f"停留 {settings['dwell_minutes']:g} 分钟沿用任务设定，尚未用行为观察校准；活动依据本身不能给出时长。",
         'output': '聚集位置与平均停留时长；与到达曲线共同用于下游计算在场人数、空间密度和离场。'},
    ]
    valid_points = {p['id'] for p in portrait.get('visualizations', {}).get('spatial_points', [])}
    valid_evidence = {e['id'] for e in portrait.get('evidence', [])}
    if result['transport_use'] == 'context':
        result['relations'][1].update({'from': '周边道路路况', 'to': '到达条件', 'label': '观察周边可达性', 'status': '高德当前路况 · 时间单独记录',
            'evidence_ids': ['amap_traffic'], 'evidence': '查询高德官方实时交通图层；道路红黄绿状态、连接状态与图层读取时间在地图上展示。',
            'assumption': '当前路况与研究时段分别记录。路况到到访量的换算关系尚待本场地资料校准。',
            'output': '先提供周边可达性与潜在延迟线索，当前人数交通系数为 1；不使用原模拟断面客流推算人数。'})
    if 'official_monument' not in valid_evidence:
        activity = result['relations'][-1]
        activity.update({'from': '场景活动', 'status': '任务设定 · 待补依据',
                         'evidence': '当前任务的活动目标与聚集位置；请补充活动类型与行为观察依据。',
                         'evidence_ids': ['scope']})
    for relation in result['relations']:
        relation['point_ids'] = [p for p in relation['point_ids'] if p in valid_points]
        relation['evidence_ids'] = [e for e in relation['evidence_ids'] if e in valid_evidence]
    return result
