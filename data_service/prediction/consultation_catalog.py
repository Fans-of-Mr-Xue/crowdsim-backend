"""Shared product contract for the model library, consultation and experiments.

Only measured/scenario data belong in common_inputs. Solver assumptions live in
the experiment, and are never presented as observations supplied by the user.
"""
from copy import deepcopy
from .runtime_budget import RUNTIME_NOTE, runtime_budget

CATALOG_VERSION = 'crowd-pre-event-5'
COMPARISON_NOTE = '共同场地、客流、驻留、通道服务约束与统计口径；比较结果差异。没有现场观测时不能判断哪个更准确。'
MODEL_DETAILS = {
    'conservation': {
        'principle': '把可行走区域分成相连的小区，每一步计算流入、流出和剩余人数。相邻小区拥挤或通道能力不足时，人会积累、排队。它计算每个小区有多少人，不生成每个人的真实轨迹。',
        'suitable_for': '先看人数何时累积、哪段通道形成瓶颈，作为容易解释和核查的比较基线。',
        'required_inputs': '共同场地与客流资料；可用步道中心线加有效宽度生成可行走面，也可直接提供可行走面。需有连接关系、出入口和目的地。',
        'outputs': ['分区人数、密度和平均速度随时间的变化', '入口外排队人数、累计离场人数、通道流量', '同一统计网格上的密度图；不输出个体轨迹'],
        'parameter_keys': ['jam_density_p_m2', 'backward_wave_speed_mps', 'counterflow_penalty'],
        'tradeoff': '快、可检查人数守恒，适合宏观基线；看不到个体避让。本平台借用车辆 CTM 的离散守恒思路作行人适配，不是已标定的行人 CTM 标准实现。',
        'citation': 'Daganzo (1994), The cell transmission model: A dynamic representation of highway traffic consistent with the hydrodynamic theory.',
    },
    'collision_free_speed': {
        'principle': '把每个人放进可行走空间，朝目标移动。前方空间足够就按期望速度走，前方有人就根据净间距减速；还会调整方向避让人和边界。可以观察同样的客流在狭窄步道怎样排队和通过。',
        'suitable_for': '关注人如何走、窄通道怎样排队，以及避让机制带来的密度变化时使用。',
        'required_inputs': '共同资料之外，必须能构成连续可行走面并扣除障碍物；入口、目标点应落在可达空间。只有地图上的一条线或场地外框不够。已有步道及宽度可由后端转换，不必重复上传。',
        'outputs': ['逐人的采样位置，可播放移动过程', '按共同网格汇总的密度、人数和速度', '入口外排队、通道流量和累计离场量'],
        'parameter_keys': ['agent_radius_m', 'cfs_time_gap_s'],
        'tradeoff': '比守恒法更细，但更依赖几何和个人空间参数。间距时间越大通常越保守；“无碰撞”是模型设计目标，不代表已验证现场安全。',
        'citation': 'Tordeux, Chraibi & Seyfried (2015), Collision-free speed model for pedestrian dynamics, TGF’15, arXiv:1512.05597.',
    },
    'social_force': {
        'principle': '把“想往目标走”和“想与别人、墙体保持距离”写成速度变化规则，每一步更新人的速度和位置。它与 CFS 用不同机制描述避让，适合比较同一场地的绕行、减速和聚集位置是否一致。',
        'suitable_for': '作为第二种个体运动机制，与 CFS 和守恒基线并排比较；检验结论对模型选择是否敏感。',
        'required_inputs': '与 CFS 共用可行走面、障碍物、初始人数、到达过程、入口和目的地，不另向用户索要一套场地资料。',
        'outputs': ['逐人的采样位置和速度变化', '按共同网格汇总的密度与人数', '入口外排队、通道流量和累计离场量；不输出可信的人体挤压力或伤害预测'],
        'parameter_keys': ['agent_radius_m', 'sfm_reaction_time_s'],
        'tradeoff': '参数对局部运动影响较大，复杂不代表更准确。本版使用 JuPedSim 的平滑排斥实现；“社会力”不能解释成真实接触力，不能据此预测踩踏伤亡。',
        'citation': 'Helbing & Molnár (1995), Social force model for pedestrian dynamics, Physical Review E 51, 4282–4286.',
    },
}

COMMON_INPUTS = [
    {'id': 'boundary', 'name': '场地边界', 'detail': '确认本次研究区域；已选环境或已绘制边界可复用，不必重新提供。边界不等于整个内部都能走。'},
    {'id': 'walkable', 'name': '步道宽度与可行走面', 'detail': '提供可行走面，或已有步道中心线及有效宽度。上海人民英雄纪念塔可复用已有 SUMO 路网；库内宽度仍需核对，不能当作实测。'},
    {'id': 'obstacles', 'name': '障碍物与不可通行区域', 'detail': '核对建筑、围栏等是否已包含在场地几何中；可以明确“已包含”或“无额外障碍”。未知不能直接当作没有。'},
    {'id': 'initial', 'name': '初始人数', 'detail': '研究开始时场内有多少人、数据来自哪里。当前首版在可行走空间内按固定种子抽样位置；精确分区初始分布尚未接入。'},
    {'id': 'arrivals', 'name': '分时到达人流', 'detail': '提供人/分钟的到达率或分时序列，明确时刻与来源；不要用整场累计参与人数代替到达率。'},
    {'id': 'destinations', 'name': '出入口与聚集目标', 'detail': '核对已有路网中的入口、离场通道、入口来流比例和聚集目标点。模拟编号不等于官方出口名称。'},
    {'id': 'dwell', 'name': '驻留与离场规律', 'detail': '提供平均停留时间或估计依据；当前以停留时长分布驱动离场，不直接导入任意离场曲线。未知时可讨论明确标注的情景假设。'},
]

# Defaults are engineering assumptions. Neither a literature citation nor this
# catalog makes them measured values for the Shanghai site.
PARAMETERS = [
    ('repetitions', '实验重复次数', '次', 1, 1, [], '每个选中模型独立运行 1–5 次；第 k 次使用基础种子加 k−1，同一轮各模型共用种子。保留逐次结果及均值、范围；计算时间随次数增加。'),
    ('grid_m', '统计网格边长', 'm', 12, 1, [], '三种方法用相同网格比较密度；网格越细计算量越大。'),
    ('sample_minutes', '结果采样间隔', '分钟', 5, 1, [], '控制输出频率，不是个体模型积分步长。'),
    ('seed', '随机种子', '', 42, 1, [], '控制初始位置和随机驻留，便于重复与公平比较。'),
    ('free_speed_mps', '自由步行速度', 'm/s', 1.34, .05, [], '无拥挤时的期望速度；默认值待场地标定。'),
    ('gate_capacity_per_m_s', '单位宽度通道服务率', '人/(m·s)', 1.3, .1, [], '共同出口服务上限；有效宽度来自场地资料。'),
    ('warning_density_p_m2', '密度提示阈值', '人/㎡', 4, .1, [], '只用于识别超阈时段，不是事故概率或安全认证。'),
    ('jam_density_p_m2', '拥挤密度上限', '人/㎡', 6, .1, ['conservation'], '守恒法容量计算的假设上限，也须大于共同提示阈值。'),
    ('backward_wave_speed_mps', '拥堵波速度', 'm/s', .6, .05, ['conservation'], '影响拥挤区域向邻区接收人流的能力。'),
    ('counterflow_penalty', '对向流折减系数', '', .35, .05, ['conservation'], '守恒法中对向流造成的容量折减假设。'),
    ('agent_radius_m', '个体半径', 'm', .2, .01, ['collision_free_speed', 'social_force'], '两种个体模型共用的身体空间近似。'),
    ('cfs_time_gap_s', 'CFS 间距时间', 's', 1., .1, ['collision_free_speed'], '控制净间距对应的可行速度，影响排队间距和通过率。'),
    ('sfm_reaction_time_s', 'SFM 速度松弛时间', 's', .5, .05, ['social_force'], '控制速度接近期望速度的快慢。'),
]


def experiment_defaults():
    return {row[0]: row[3] for row in PARAMETERS}


def experiment_schema():
    from .inputs import BOUNDS
    return {'type': 'object', 'properties': {
        'model_ids': {'type': 'array', 'maxItems': 3, 'uniqueItems': True,
                      'items': {'type': 'string', 'enum': list(MODEL_DETAILS)}},
        'settings': {'type': 'object', 'properties': {
            key: {'type': 'integer' if key in ('seed', 'repetitions') else 'number', 'minimum': BOUNDS[key][0], 'maximum': BOUNDS[key][1]}
            for key, *_ in PARAMETERS}},
        'notes': {'type': 'string', 'maxLength': 4000},
    }, 'required': ['model_ids', 'settings', 'notes']}


def consultation_catalog():
    from .catalog import method_catalog, METHODS
    from .inputs import BOUNDS
    return {'version': CATALOG_VERSION, 'methods': method_catalog(), 'common_inputs': deepcopy(COMMON_INPUTS),
            'parameters': [dict(key=key, label=label, unit=unit, default=default, step=step,
                                models=models, description=description, min=BOUNDS[key][0], max=BOUNDS[key][1])
                           for key, label, unit, default, step, models, description in PARAMETERS],
            'experiment_schema': experiment_schema(),
            'superseded_model_names': [row['name'] for row in METHODS if not row.get('recommended')] + ['路网流量守恒'],
            'explanation': model_explanation(), 'comparison_note': COMPARISON_NOTE, 'runtime_note': RUNTIME_NOTE, 'runtime_budget': runtime_budget()}


def common_input_rows(values=None, state=None):
    rows = [dict(name=row['name'], detail=row['detail'], source='unknown', status='pending') for row in COMMON_INPUTS]
    if (values or {}).get('task.environment') or (state or {}).get('region', {}).get('geometry'):
        rows[0].update(status='available', source='user', detail='已在当前任务选择或绘制研究范围；复用右侧地图边界，不代表内部全部可行走。')
    return rows


def model_explanation(model_ids=None):
    from .catalog import method_catalog
    selected = set(model_ids or MODEL_DETAILS)
    parts = ['这几种模型使用同一套场地和客流资料，区别在于怎样描述人群移动。']
    for row in method_catalog():
        if row['id'] not in selected:
            continue
        parts.append(f"**{row['name']}**\n\n{row['principle']}\n\n适合解决：{row['suitable_for']}\n\n需要输入：{row['required_inputs']}\n\n输出：{'；'.join(row['outputs'])}。\n\n使用限制：{row['tradeoff']}\n\n依据：[{row['citation']}]({row['source']})")
    parts.append('**模型选择与实验配置**\n\n通用输入包括边界、步道宽度或可行走面、障碍物、初始人数、分时到达、出入口与目标、驻留规律。已有资料只核对一次。第 05 项“模型选择”确定比较哪些模型；经过领域模型整理场地与数据后，在独立的“实验控制”页面配置重复次数、网格、采样间隔、随机种子和模型参数，再主动运行。默认值是可修改的工程假设；态势研判展示实验结果与模型对比。')
    parts.extend([COMPARISON_NOTE, RUNTIME_NOTE])
    return '\n\n'.join(parts)
