"""Ordinal support for a particular task claim. Recomputed on portrait reads."""
LABELS = ('无观测支持', '间接参照', '相关依据', '直接依据')
HISTORICAL_CONTEXT = {
    '历史活动记录': '本次可能到访的人群与活动',
    '官方历史活动发布': '周边活动对本次到访的影响',
    '历史管理资料': '本次人流组织条件',
    '运营方历史公告': '本次交通接驳条件',
    '官方历史公告': '本次活动安排',
    '官方推荐 · 侧写线索': '游客在此停留的可能动机',
}


def rating(score, claim, reason):
    return dict(score=score, label='待评' if score is None else LABELS[score],
                claim=claim, reason=reason)


def notice_strength(row, decision):
    claim = decision['title'] + '是否举办' if decision else '本次活动是否举办'
    if row.get('evidence_origin') == 'scenario' or row['id'] == 'demo-countdown-cancellation':
        return rating(0, claim, '模拟公告用于演示活动变化。')
    if not decision:
        return rating(None, claim, '尚无与当前任务、活动和范围匹配的判定。')
    if decision['status'] == 'conflict':
        return rating(None, claim, '关联公告对举办状态有冲突，须核对原文和发布时间。')
    if row['activity_notice']['authority'] == 'organizer':
        if row['id'] not in decision['decisive_ids']:
            return rating(0, claim, '同一活动已有更新的主办方公告。')
        if row.get('url'):
            return rating(3, claim, '记录为主办方或主管单位发布，已关联本次活动，并提供原文链接。')
        return rating(2, claim, '已关联具名主办方公告，原文链接待补。')
    return rating(1, claim, '其他来源报道了本次活动，举办状态仍以主办方公告核对。')


def portrait_strengths(portrait, roads):
    demand = portrait['demand']
    config = portrait.get('demand_inputs') or {}
    decisions = demand.get('activity_decisions', [])
    by_notice = {n['id']: decision for decision in decisions for n in decision['notices']}
    historical_source = (config.get('history') or {}).get('evidence_id')
    scores = {}
    for row in portrait['evidence']:
        identifier, kind = row['id'], row.get('kind')
        if row.get('activity_notice') or row.get('notice_suggestion') or row.get('evidence_origin') == 'scenario':
            item = notice_strength(row, by_notice.get(identifier))
        elif kind in ('往年跨年通报', '历史统计') or identifier == historical_source:
            item = rating(1, '本次场地到访规模', '历史统计提供客流量级，换算本次场地和时段仍需本地计数。')
        elif identifier == 'boundary':
            item = rating(3, '本次分析边界', '采用当前任务保存的研究区域几何。')
        elif identifier == 'roads':
            item = (rating(3, '本次仿真采用的通道几何', '任务绑定的路网已载入，通道宽度和开放情况需另行核对。')
                    if (roads or {}).get('features') else rating(None, '本次仿真采用的通道几何', '当前未读取到路段。'))
        elif identifier == 'scope' or kind == '假设':
            item = rating(0, '本次实际客流', '任务设定用于构造情景，实际客流需观测支持。')
        elif isinstance(kind, str) and kind in HISTORICAL_CONTEXT:
            item = rating(1, HISTORICAL_CONTEXT[kind], '提供往年情况或行为线索，本次现场情况需另行核实。')
        else:
            item = rating(None, '对本次研判的支持', '需核对具体判断、资料时段和覆盖范围。')
        scores['source:' + identifier] = item

    gate_claim = '本次各入口分时到达人数'
    counts = portrait.get('gate_counts')
    if not demand.get('gate_counts'):
        reason = '计数资料需重新核对。' if counts else '尚未导入覆盖研究时段的入口计数。'
        scores['gateCounts'] = rating(None, gate_claim, reason)
    else:
        levels = {'observed': (3, '标为本时段实测，各入口记录已通过时空范围和完整性校验。'),
                  'reference': (2, '相同入口的可比时段记录已通过校验，本次客流仍需估计。'),
                  'scenario': (0, '计数来自情景样例。')}
        score, reason = levels[counts['kind']]
        scores['gateCounts'] = rating(score, gate_claim, reason)
    for decision in decisions:
        scores['assumption:' + decision['activity_id']] = rating(0, '活动新增到访人数', '活动规模和到访比例来自情景设定。')
    if demand.get('status') == 'ready' and not demand.get('gate_counts'):
        for key, claim in [('weather', '天气对客流的修正'), ('transport', '交通对到达客流的修正')]:
            kind = (config.get(key) or {}).get('kind')
            if kind in ('scenario', 'simulated'):
                scores[key] = rating(0, claim, '计算采用情景设定。')
            elif kind in ('observed', 'forecast'):
                scores[key] = rating(1, claim, '天气或交通资料提供间接依据，到访换算关系仍需本地客流校验。')
    return scores
