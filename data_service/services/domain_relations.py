"""Activity evidence and transparent upstream modelling inputs; no solver runs."""
from datetime import datetime, timedelta
import math

from ..prediction.arrivals import ArrivalProfile
from ..prediction.inputs import task_times


def activity_records(task, portrait, news=()):
    try:
        start, end, _ = task_times(task)
    except (ValueError, TypeError, KeyError):
        start = end = None
    evidence = {row['id']: row for row in portrait.get('evidence', [])}
    records = []
    specs = [
        ('walking_event', '途经纪念塔的公益健步走', '2026-09-08', '2026-09-08', '北外滩 → 外白渡桥 → 纪念塔附近', 200, '近 200 人参与整场活动；纪念塔到访份额及经过时刻未给出。', '团队到达、北侧接入、短时停留'),
        ('news_activities_2026', '北外滩打卡、冰雪季与音乐会', '2025-12-30', '2026-03-03', '北外滩商圈，场地外部', None, '官方迎新活动汇总提及这些活动，未给出各场时刻与人数。', '周边吸引或分流；到访转化与步行延迟'),
    ]
    for source_id, title, first, last, location, amount, fact, use in specs:
        source = evidence.get(source_id)
        if not source:
            continue
        matches = bool(start and first <= end.date().isoformat() and last >= start.date().isoformat())
        records.append(dict(id=source_id, source_id=source_id, title=title, start_date=first, end_date=last,
                            location=location, reported_attendance=amount, fact=fact, model_use=use,
                            source=source.get('source'), url=source.get('url'), published=source.get('date'),
                            time_matches=matches, applicability='时段有交集 · 局部规模待核' if matches else '历史参考 · 不在本次时段',
                            counted=False))
    urls = {r['url'] for r in records}
    for article in news:
        if not article.get('url') or article['url'] in urls:
            continue
        urls.add(article['url'])
        records.append(dict(id='news_' + str(article['_id']), source_id='news_' + str(article['_id']),
                            title=article.get('title', '活动报道'), source=article.get('source', '新闻库'), url=article['url'],
                            published=str(article.get('publishedAt', '')), start_date=None, end_date=None,
                            location='地点待核对', reported_attendance=None,
                            fact=str(article.get('summary', ''))[:450], model_use='先核对活动日期、地点、规模与本场地关系',
                            time_matches=False, applicability='待核对活动日期 · 发稿时间不代表活动时间', counted=False))
    configured = (portrait.get('demand_inputs') or {}).get('activities', [])
    known = {row['source_id'] for row in records}
    for activity in configured:
        if activity['source_id'] in known:
            continue
        records.append(dict(id=activity['source_id'], source_id=activity['source_id'], title=activity['title'],
                            source=activity['source_label'], url=activity['source_url'], published=None,
                            start_date=activity['start'][:10], end_date=activity['end'][:10],
                            location='用户设置的本场地到访情景', reported_attendance=activity['attendance'],
                            fact=activity['note'], model_use='按参与人数、场地到访份额与新增比例换算到达需求',
                            time_matches=True, applicability='用户设置 · 数值依据见说明', counted=False))
    decisions = {row['activity_id']: row for row in portrait.get('demand', {}).get('activity_decisions', [])}
    used = {a['source_id'] for a in configured if decisions.get(a['id'], {}).get('included')}
    for row in records:
        row['counted'] = row['source_id'] in used
    return {'records': records, 'configured_count': len(used), 'note': '报道人数先换算为到访本场地的新增需求；重复报道或已含在基准中的人群不重复计入。'}


def clue_input_relations(clues):
    """Organise conditions by what they can inform, without applying constraints."""
    mappings = {
        'clue_metro': dict(group='places', input_label='封站安排 → 接入方向与开放条件',
                          target_label='分时入口可用性、来流比例', input_gap='本次封站公告与接入流量；尚不调整入口比例'),
        'clue_lights': dict(group='behavior', input_label='关灯时刻 → 观景结束与离场阶段',
                           target_label='观景结束时刻、离场节奏', input_gap='本次启闭公告与离场计数；尚不设置集中离场'),
        'clue_expectation': dict(group='behavior', input_label='倒计时安排 → 等待动机与停留时长',
                                target_label='到访目的、停留时间', input_gap='本次活动安排与停留观察；尚不改变停留时长'),
    }
    return {**clues, 'items': [dict(row, **mappings[row['id']], adoption='未采用为数值或约束')
                              if row['id'] in mappings else row for row in clues.get('items', [])]}


def occupancy_estimate(task, demand, settings):
    """A fixed-stay stock identity, explicitly distinct from spatial simulation."""
    dwell = float(settings.get('dwell_minutes') or 0)
    initial = float(settings.get('initial_crowd') or 0)
    if dwell <= 0 or demand.get('status') != 'ready':
        return None
    start, _, duration = task_times(task)
    profile = ArrivalProfile(demand['forecast_inputs'], duration)
    edges = [b['minute'] for b in demand['bins']] + [duration]
    # Changes in slope occur at arrivals, departures shifted by dwell, and the
    # initial occupants' remaining stay bound. Include all to preserve the peak.
    times = {0., duration, min(duration, dwell)}
    times.update(x for edge in edges for x in (edge, edge+dwell) if 0 <= x <= duration)
    step = max(1, duration/200)
    times.update(min(duration, i*step) for i in range(math.ceil(duration/step)+1))
    points = []
    for t in sorted(times):
        arrivals = profile.cumulative(t)
        left = profile.cumulative(max(0., t-dwell)) + initial * min(1., t/dwell)
        points.append({'minute': t, 'at': (start+timedelta(minutes=t)).isoformat(), 'count': max(0., initial+arrivals-left)})
    peak = max(points, key=lambda p:p['count'])
    return {'method':'fixed-stay-planning-balance-v1', 'points':points, 'peak':peak['count'], 'peak_at':peak['at'],
            'final':points[-1]['count'], 'dwell_minutes':dwell,
            'formula':'在场 = 起始尚未离场人数 + 累计到达 − 新到访者累计离场',
            'assumption':f'到达即入场，新到访者固定停留 {dwell:g} 分钟；起始人群的剩余停留时间均匀分布在 0–{dwell:g} 分钟。',
            'note':'这是领域模型的在场规模粗估，未计通道拥堵、排队及行为差异；不替代后续空间仿真。'}


def model_inputs(task, portrait, roads=None):
    settings, demand = portrait['scene']['settings'], portrait['demand']
    activity_count = demand.get('activity_count', 0)
    exits = settings.get('exit_points', {})
    shares = settings.get('entrance_shares', [])
    records = [
        dict(id='arrivals', name='到访规模与时序', value=f"{demand['total_visits']:,} 人次 / 研究时段" if demand.get('status')=='ready' else '待建立',
             basis=f'历史统计 + 范围换算 + 天气情景 + {activity_count} 项活动设置', status='情景估计',
             assumption='换算比例与天气系数未标定；新闻活动须先确认本场地贡献。', output='累计到达需求、分时到达率', target='arrival_schedule', evidence_ids=['bund_oct_2025'], edit='demand'),
        dict(id='initial', name='起始在场人数', value=f"{settings.get('initial_crowd',0):g} 人", basis='本任务起始人数设定', status='任务假设',
             assumption='应由起始时刻计数或已有观测替换，不能把全天人次填在这里。', output='仿真起始人数', target='initial_crowd', evidence_ids=['scope'], edit='scene'),
        dict(id='stay', name='停留与离场', value=f"{settings.get('dwell_minutes',0):g} 分钟", basis='观景、摄影活动依据 + 任务停留设定', status='行为假设',
             assumption='活动类型有资料支持；时长暂沿用任务参数，需停留观察校准。', output='平均停留时长；配合到达率估计在场规模', target='dwell_minutes', evidence_ids=['photo_guide','official_monument'], edit='scene'),
        dict(id='access', name='来流入口与分配', value=' / '.join(f'{name} {shares[i]*100:g}%' if i<len(shares) else name for i,name in enumerate(exits)),
             basis='当前路网接入坐标 + 场景比例', status='位置有据 · 比例待核', assumption='道路拥堵、封站公告只能提示方向变化，尚不能确定分配比例。',
             output='入口坐标、来流比例；统一交给各模型', target='exit_points / entrance_shares', evidence_ids=['roads','metro_nye_2025'], edit='scene'),
        dict(id='attraction', name='活动与聚集位置', value='已关联活动吸引位置' if settings.get('attraction_point') else '位置待补充', basis='场所介绍、摄影推荐与场地坐标', status='位置关系',
             assumption='位置表达活动动机，不能据此生成实测密度。', output='个体运动目标点；观察聚集区域', target='attraction_point', evidence_ids=['official_monument','photo_guide'], edit='places'),
        dict(id='walkable', name='可通行空间与宽度', value=f"{len((roads or {}).get('features', []))} 条路段 · 共用宽度 {settings.get('path_width_m',0):g} m", basis='任务路网版本 + 有来源的路段宽度 / 缺省宽度', status='几何资料 + 工程假设',
             assumption='缺省宽度不等于逐路实测；实际可行走面和障碍物仍需补齐。', output='路网、有效通道宽度；供容量和运动计算', target='road_network / path_width_m', evidence_ids=['roads'], edit='scene'),
        dict(id='movement', name='人群构成与行走', value=f"自由步速 {settings.get('free_speed_mps',0):g} m/s", basis='用户人群画像 + 当前通用步速', status='任务假设',
             assumption='尚未根据老幼比例等资料标定分组步速，当前只传递共用步速。', output='共用自由步速；画像供行为差异研究', target='free_speed_mps', evidence_ids=['scope'], edit='scene'),
        dict(id='availability', name='开放时段与外部条件', value='实时路况 + 历史公告', basis='高德当前路况、往年亮灯与地铁公告', status='待核对本次安排',
             assumption='历史启闭时刻未确认适用于本次，暂不自动封路或修改研究时间。', output='活动阶段、时段约束的待确认项', target=None, evidence_ids=['amap_traffic','lighting_nye_2025','metro_nye_2025'], edit='clues'),
    ]
    counts = demand.get('gate_counts')
    if counts:
        records[0].update(basis=counts['source'], status=counts['kind'], evidence_ids=[],
                          assumption='已覆盖各入口全部到访，活动人数不另加。')
        if counts['entered']:
            records[3].update(value=' / '.join(f'{g} {n/counts["entered"]:.0%}' for g,n in counts['by_gate'].items()),
                              basis=counts['source'], status='全时段进入人数占比', evidence_ids=[])
    valid = {e['id'] for e in portrait.get('evidence', [])}
    for record in records:
        record['evidence_ids'] = [eid for eid in record['evidence_ids'] if eid in valid]
    return {'records':records, 'note':'已支持的字段供同一任务下的模型共用；待确认的约束保留为线索，不伪装成已执行参数。'}
