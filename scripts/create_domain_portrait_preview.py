"""Create an independent, evidence-first domain portrait; never update the source task."""
import argparse
import copy
import json
from pathlib import Path
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-task', required=True)
    parser.add_argument('--base', default='http://127.0.0.1:8768/api/crowdSim')
    parser.add_argument('--record', default='/private/tmp/crowdsim-domain-portrait-preview.json')
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(path, payload=None):
        request = urllib.request.Request(args.base + path, data=None if payload is None else json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=120) as response:
            return json.load(response)['data']

    record = Path(args.record)
    if record.exists():
        print(record.read_text())
        return
    source = api('/analysis-tasks/' + args.source_task)
    region = copy.deepcopy(source['region'])
    environment_id, environment_revision = region.get('environment_id'), region.get('environment_revision')
    # Copy the saved boundary, without changing or republishing the shared environment.
    region['mode'] = 'custom'
    dimensions = [
        dict(id='demand', short='活动吸引', title='活动吸引与到访需求', question='为什么来，可能在什么时候集中到访？',
             judgment='纪念塔与沿江空间可作为活动吸引因素的研究对象；实际到访规模与高峰时刻尚无现场数据支撑。',
             interpretation='把景观吸引、活动安排与交通来流放在一起观察，先明确到访动机与证据，再估计人数。',
             metrics=[dict(label='活动关注变化', value='待补充', detail='需要相同口径、连续时段的关注数据；不能直接等同到访人数。'), dict(label='分时到访规模', value='待估计', detail='优先入口计数；交通客流只能估计其中一个来源。')],
             evidence=['scope', 'boundary', 'method'],
             chain=['活动安排、历史到访、上游交通客流', '统一时段与范围，估计到访转化比例', '形成到访量区间，辨别潜在集中时段'],
             gaps=['往年同类活动的分时入口计数', '活动安排及对应日期', '周边车站客流与去向抽样'],
             outputs=['到访量区间', '到达时间分布', '不同来源的贡献'],
             caveat='目前只有研究范围与情景设定；侧写换算为教学示例，不代表真实客流。'),
        dict(id='space', short='空间承载', title='空间条件与承载约束', question='哪里能走、能停留，哪些限制值得核实？',
             judgment='已有边界与步行路网，可定位通道和连接关系；可用空间、有效宽度与现场障碍仍需核对，暂不据此宣称安全容量。',
             interpretation='区分研究范围、实际可行走面与可驻留区域。面积相同的场地，连接方式和出口条件可能不同。',
             metrics=[dict(label='研究范围面积', value='boundary_area', detail='由已保存边界计算；不是净可行走面积，也不是容量。'), dict(label='库内步行通道', value='road_count', detail='读取当前任务绑定的路网；数量不代表现场畅通程度。'), dict(label='有效宽度', value='待现场核对', detail='库内宽度可能为建模参数，不能自动标为实测。')],
             evidence=['boundary', 'roads', 'method'],
             chain=['边界、道路、障碍、现场测量', '区分用途与层级，核对面积、宽度和连接', '描述可用空间与局部约束'],
             gaps=['步道及出口的现场有效宽度', '临时围栏、摊位与不可驻留区域', '逐出入口开放条件'],
             outputs=['空间分区与用途', '通道属性与约束', '可用面积与来源'],
             caveat='路网描述的是空间结构；瓶颈是否出现还取决于实际来流与活动方式。'),
        dict(id='people', short='人群活动', title='人群构成与活动方式', question='人来这里做什么，怎样使用这片空间？',
             judgment='可先区分游览拍照、通行经过、等待活动三类行为假设。其占比、驻留时长和空间偏好均待观察确认。',
             interpretation='按活动目的描述行为。候选类别帮助组织调查，不替代现场人口构成。',
             metrics=[dict(label='候选活动方式', value='3 类假设', detail='游览拍照 / 通行经过 / 等待活动；不表示已调查出三类人数。'), dict(label='各类占比与驻留', value='待观察', detail='需要抽样视频、问卷或人工观察记录。')],
             evidence=['scope', 'method'],
             chain=['行为观察、到访目的调查、历史视频', '按一致规则分类，统计占比与停留区间', '形成活动方式与空间使用画像'],
             gaps=['各活动目的的抽样占比', '不同区域的停留时长', '等待活动与集中离场的观察记录'],
             outputs=['活动类型及比例', '停留时长区间', '目的地与路线偏好'],
             caveat='行为类别目前是研究假设；没有自动映射为仿真中的人群。'),
        dict(id='access', short='内外集散', title='内外交通与集散条件', question='从哪里来，向哪里走，外部条件怎样影响场内？',
             judgment='场内步行连接已有资料；外部交通客流、接驳关系和时刻表尚未纳入，暂不能判断主要到访来源或散场去向。',
             interpretation='把场地放回周边交通环境中，分别辨识步行、公共交通及其他到访来源。',
             metrics=[dict(label='场内连接资料', value='已有路网', detail='可点击地图通道核对；并不包含完整的外部集散网络。'), dict(label='来源及去向比例', value='待补充', detail='地铁出站量需要去向抽样与步行时延，不能直接作为入园人数。')],
             evidence=['roads', 'method'],
             chain=['交通客流、步行连接、管制与时刻表', '匹配来源、去向和步行时延，避免重复计数', '描述来流来源与散场约束'],
             gaps=['周边站点与场地的实际步行连接', '活动当日运营与交通管制资料', '来访来源及离场去向抽样'],
             outputs=['来源—场地—去向关系', '到达时延', '集散条件及限制'],
             caveat='未取得活动当日资料前，不推断末班车或交通管制的具体影响。'),
        dict(id='management', short='组织管理', title='组织管理与应对条件', question='有哪些措施，覆盖什么位置与时段？',
             judgment='尚未取得本次情景的正式管理方案。入口开放、分流和人员部署可作为待核实事项，不能当作已执行的现场事实。',
             interpretation='把措施落实到执行位置、时段和责任，区分计划安排与实际执行记录。',
             metrics=[dict(label='活动管理方案', value='尚未取得', detail='需要可追溯的文件与适用日期。'), dict(label='措施覆盖范围', value='待确认', detail='包括入口开放、人员部署、标识和分流安排。')],
             evidence=['scope', 'method'],
             chain=['管理方案、部署图、历史执行记录', '核对位置、时段、责任和实施条件', '描述已有措施与覆盖缺口'],
             gaps=['活动管理方案与出入口时刻表', '人员部署、围栏和引导标识的位置', '以往执行记录与现场复核'],
             outputs=['措施清单', '空间与时间覆盖', '实施条件与待核事项'],
             caveat='不将“有管理方案”直接换算为风险下降比例。'),
    ]
    portrait = dict(schema_version=1, mode='preview', title='人群聚集领域模型',
        scope='以人民英雄纪念塔及黄浦公园步行空间为对象，研究活动吸引、空间约束、人群活动、内外集散与组织管理。',
        origin=dict(source_task_id=source['id'], source_revision=source['revision'], note='仅复制场地与研究时间范围；不继承实验参数和结果。'),
        dimensions=dimensions,
        evidence=[
            dict(id='boundary', title='黄浦公园研究边界', kind='资料', source='任务中保存的环境库第 6 版 / OpenStreetMap 边界', date=region.get('boundary_source', {}).get('retrievedAt', '')[:10], url=region.get('boundary_source', {}).get('url', ''), content='保存的 WGS84 边界用于界定研究范围。边界面积包含不同用途空间，不能直接视为净可行走面积。', dimensions=['demand', 'space']),
            dict(id='roads', title='场地步行路网与设施', kind='资料', source=f'环境库固定版本 {environment_revision}', date='', url='', content='从当前任务绑定的环境库版本读取。道路、连接与宽度保留原有属性；建模参数不等同现场测量。', dimensions=['space', 'access']),
            dict(id='scope', title='跨年聚集研究情景', kind='假设', source='本临时研究任务', date='', url='', content='沿用 2026-12-31 20:00 至次日 00:30 的研究窗口。活动方式与时间窗口为情景设定，不是已核实的活动公告。', dimensions=['demand', 'people', 'management']),
            dict(id='method', title='人群风险评估与资料组织方法', kind='方法参考', source='英国 HSE · Assess crowd safety risks and identify hazards', date='', url='https://www.hse.gov.uk/event-safety/crowd-management-assess.htm', content='参考到访规模、活动人群、场地适用性及管理措施等研究方面；它不提供本场地的观测数据或容量结论。', dimensions=[d['id'] for d in dimensions]),
        ],
        candidates=[
            dict(id='holiday', title='历史节假日旅游统计', dimension='demand', status='找到来源 · 待评估', retained=False,
                 idea='借助城市节假日旅游变化，观察活动吸引与外部到访背景。',
                 available='已找到上海市文旅局发布的 2025 年国庆中秋旅游统计。',
                 missing='数据为全市假期口径，尚无纪念塔局部、逐时数据；不能直接换算本场地人数。',
                 url='https://www.shanghai.gov.cn/nw31406/20251009/d3a47b3160214af28f1a60ff35f1d9ab.html'),
            dict(id='places', title='设施分布与步行连接', dimension='space', related=['people', 'access'], status='已有资料', retained=True,
                 idea='通过设施与连接结构，寻找潜在停留点、活动区域和通行约束。',
                 available='已读取环境库的场地边界、道路和设施。',
                 missing='有设施不等于有人停留；仍需现场观察与设施开放条件。',
                 url=region.get('boundary_source', {}).get('url', '')),
            dict(id='signal', title='运营商信令的活动客流线索', dimension='people', related=['demand'], status='找到案例 · 待评估', retained=False,
                 idea='利用聚合移动信令，侧面了解某个区域的活动规模与变化。',
                 available='官方报道介绍了 2025 年静安国际光影展区域的信令客流测算案例。',
                 missing='这是其他区域案例，没有取得本场地数据；覆盖、去重和停留口径均待核对。',
                 url='https://www.shanghai.gov.cn/nw4411/20251009/98224e98699c453fad979c0cea00422c.html'),
            dict(id='metro', title='上游车站分时出站量', dimension='access', related=['demand'], status='建议寻找', retained=False,
                 idea='把公共交通客流作为到访来源的一条侧面线索。',
                 available='已提出需要的资料类型，尚未取得站点分时客流。',
                 missing='需要站点数据、前往场地比例与步行时延；不可把全部出站人数计入场地。', url=''),
            dict(id='notice', title='活动与交通管制公告', dimension='management', related=['access'], status='建议寻找', retained=False,
                 idea='通过公告识别活动时间、入口开放与外部交通条件。',
                 available='已提出需要的资料类型，尚未取得研究当日公告。',
                 missing='需核对发布日期、适用范围与现场执行情况。', url=''),
        ],
        proxy_example=dict(station_flow=1200, destination_share=20, walk_minutes=10, note='全部数值均为演示假设，仅说明从上游客流换算一个来源的到访量。'))
    payload = dict(title='纪念塔 · 五维领域模型探索（临时）', summary=portrait['scope'], event_id='festival-gathering', event_label='节庆活动聚集',
                   time_window=source['time_window'], template_case='shanghai-bund-new-year', status='draft', region=region,
                   population={}, references={'items': []}, components={'domain_portrait': portrait, 'road_network_binding': {'environment_id': environment_id, 'environment_revision': environment_revision}})
    saved = api('/analysis-tasks', payload)
    result = dict(task_id=saved['id'], task_revision=saved['revision'], url=f"http://localhost:8080/home/crowdsim/preAnal/domainPortrait?task={saved['id']}&case=shanghai-bund-new-year")
    record.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    after = api('/analysis-tasks/' + args.source_task)
    assert all(after[k] == source[k] for k in ('revision', 'updated_at', 'components', 'region')), 'Source task changed concurrently; inspect before proceeding.'
    print(json.dumps(result, ensure_ascii=False))
    print('Source task unchanged; no simulations submitted.')


if __name__ == '__main__':
    main()
