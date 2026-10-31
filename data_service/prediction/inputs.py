"""Resolve a single, database-authoritative input for every comparison method."""
import copy
import math
import re
from datetime import datetime, timedelta
from .catalog import method_catalog

BOUNDS = {
    'initial_crowd': (0, 100000), 'arrival_rate_per_minute': (0, 3000),
    'free_speed_mps': (.2, 2.5), 'jam_density_p_m2': (2, 10),
    'warning_density_p_m2': (.5, 9), 'dwell_minutes': (1, 240),
    'path_width_m': (1, 30), 'grid_m': (4, 30), 'sample_minutes': (1, 30),
    'emergency_open_minute': (-1, 720), 'response_delay_minutes': (0, 60),
    'familiarity': (0, 1), 'guidance_compliance': (0, 1),
    'route_sensitivity': (.01, 3), 'friction_probability': (0, 1),
    'counterflow_penalty': (0, .9), 'initial_stationary_fraction': (0, 1),
    'seed': (0, 2147483647), 'repetitions': (1, 5),
    'gate_capacity_per_m_s': (.1, 3), 'backward_wave_speed_mps': (.1, 1.5),
    'agent_radius_m': (.15, .35), 'cfs_time_gap_s': (.5, 3), 'sfm_reaction_time_s': (.2, 1.5),
}


def task_times(task):
    text = task.get('time_window', '')
    dates = re.findall(r'\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?', text)
    if len(dates) >= 2:
        start, end = (datetime.fromisoformat(value) for value in dates[:2])
    elif len(dates) == 1:
        start = datetime.fromisoformat(dates[0])
        tail = text[text.find(dates[0]) + len(dates[0]):]
        clock = re.search(r'(\d{1,2}):(\d{2})', tail)
        if not clock:
            raise ValueError('时间窗请填写起止时间，如 2014-12-31 20:00 至 2015-01-01 01:20')
        end = start.replace(hour=int(clock[1]), minute=int(clock[2]), second=0)
        if end <= start:
            end += timedelta(days=1)
    else:
        raise ValueError('动态预测需要任务中保存的完整起止时间，不能使用静态案例时间')
    minutes = (end - start).total_seconds() / 60
    if not 1 <= minutes <= 720:
        raise ValueError('人群路网预测支持 1 分钟至 12 小时的任务，请缩小时间窗')
    return start, end, minutes


def default_settings(task):
    _, _, duration = task_times(task)
    forecast = task.get('components', {}).get('forecast_parameters', {})
    form = task.get('components', {}).get('definition_ui', {}).get('parameter_form', {})
    initial = forecast.get('initialState') or {}
    snapshot = forecast.get('predictionSnapshot') or {}
    if not isinstance(initial, dict) or not isinstance(snapshot, dict):
        raise ValueError('任务中的初始状态与预测参数须为对象，请重新保存任务定义')
    shares = initial.get('entranceShares') or {}
    if not isinstance(shares, dict):
        raise ValueError('任务中的入口比例须为对象，请重新保存任务定义')
    initial_crowd = initial.get('crowd', 0)
    estimated = snapshot.get('estimatedParticipants', form.get('estimatedParticipants', initial_crowd))
    settings = {
        'initial_crowd': initial_crowd,
        'arrival_rate_per_minute': round(max(0, estimated - initial_crowd) / duration, 2),
        'free_speed_mps': initial.get('speed', 1.34), 'jam_density_p_m2': 6,
        'warning_density_p_m2': forecast.get('densityThreshold', 4),
        'dwell_minutes': 30, 'path_width_m': 4, 'grid_m': 12, 'sample_minutes': 5,
        'emergency_open_minute': 0 if initial.get('entranceCOpen') else -1, 'response_delay_minutes': 5,
        'familiarity': .5, 'guidance_compliance': .7, 'route_sensitivity': .1,
        'friction_probability': .2, 'counterflow_penalty': .35,
        'initial_stationary_fraction': .5, 'seed': 42,
        'gate_capacity_per_m_s': 1.3, 'backward_wave_speed_mps': .6,
        'agent_radius_m': .2, 'cfs_time_gap_s': 1., 'sfm_reaction_time_s': .5,
        'allow_boundary_walkable': False,
        'attraction_point': None,
        'exit_points': {}, 'exit_labels': {}, 'exit_points_note': '', 'arrival_interpolation': 'smooth',
        'entrance_shares': [shares.get(key, fallback) for key, fallback in (('A', .7), ('B', .3), ('C', 0))], 'arrival_schedule': [],
    }
    from .consultation_catalog import experiment_defaults
    settings.update(experiment_defaults())
    settings['free_speed_mps'] = initial.get('speed', settings['free_speed_mps'])
    settings['warning_density_p_m2'] = forecast.get('densityThreshold', settings['warning_density_p_m2'])
    settings.update(copy.deepcopy(task.get('components', {}).get('research_plan', {}).get('experiment', {}).get('settings', {})))
    settings.update(copy.deepcopy(task.get('components', {}).get('forecast_setup', {})))
    return settings


def normalize_settings(task, overrides=None):
    settings = default_settings(task)
    if overrides:
        if not isinstance(overrides, dict) or set(overrides) - set(settings):
            raise ValueError('预测配置包含不支持的字段')
        settings.update(copy.deepcopy(overrides))
    for key, (low, high) in BOUNDS.items():
        value = settings.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} 必须在 {low}–{high} 之间')
    if settings['warning_density_p_m2'] >= settings['jam_density_p_m2']:
        raise ValueError('密度预警值应小于拥挤密度上限')
    for key in ('seed', 'initial_crowd', 'repetitions'):
        if settings[key] != int(settings[key]):
            raise ValueError(f'{key} 必须为整数')
        settings[key] = int(settings[key])
    if type(settings['allow_boundary_walkable']) is not bool:
        raise ValueError('请明确是否将区域内部假设为可行走空间')
    points = settings['exit_points']
    if settings['attraction_point'] is not None:
        from ..schemas.environments import coordinate
        settings['attraction_point'] = coordinate(settings['attraction_point'])
    if not isinstance(points, dict) or len(points) > 12 or any(key not in 'ABCDEFGHIJKL' or len(key) != 1 for key in points):
        raise ValueError('通道须使用 A–L 编号，最多 12 个；编号仅用于模拟，不代表官方出口编号')
    if points:
        from ..schemas.environments import coordinate
        points = {key: coordinate(value) for key, value in points.items()}
        if len({tuple(value) for value in points.values()}) != len(points):
            raise ValueError('通道的位置不能相同')
        settings['exit_points'] = dict(sorted(points.items()))
    labels = settings['exit_labels']
    if not isinstance(labels, dict) or any(key not in points or not isinstance(value, str) or not value.strip() or len(value) > 100 for key, value in labels.items()):
        raise ValueError('通道名称须对应已配置坐标，且为不超过 100 字的文本')
    if not isinstance(settings['exit_points_note'], str) or len(settings['exit_points_note']) > 500:
        raise ValueError('出口位置说明须为不超过 500 字的文本')
    shares = settings['entrance_shares']
    if not isinstance(shares, list) or len(shares) != (len(points) or 3) or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in shares) or abs(sum(shares) - 1) > .001:
        raise ValueError('来流比例须与通道按编号排序一一对应，每项在 0–1 之间，合计为 1')
    schedule = settings['arrival_schedule']
    if settings['arrival_interpolation'] not in ('smooth', 'step'):
        raise ValueError('到达曲线连接方式须为 smooth（平滑曲线）或 step（阶梯时段）')
    if not isinstance(schedule, list) or len(schedule) > 200:
        raise ValueError('到达曲线最多包含 200 个时间点')
    last = -1
    duration = task_times(task)[2]
    for row in schedule:
        if not isinstance(row, dict) or set(row) != {'minute', 'rate'} or any(type(row[k]) not in (int, float) or not math.isfinite(row[k]) for k in ('minute', 'rate')):
            raise ValueError('到达曲线格式须为 [{minute: 分钟, rate: 人/分钟}]')
        if not last < row['minute'] <= duration or not 0 <= row['rate'] <= 3000:
            raise ValueError('到达曲线时间须递增且位于任务时间窗内，来流为 0–3000 人/分钟')
        last = row['minute']
    if schedule and schedule[0]['minute'] != 0:
        raise ValueError('到达曲线必须从第 0 分钟开始')
    return settings


def normalize_methods(methods, task=None):
    catalog = {method['id'] for method in method_catalog(task, include_legacy=True)}
    if not isinstance(methods, list) or not methods or len(methods) > len(catalog) or any(not isinstance(v, str) or v not in catalog for v in methods) or len(set(methods)) != len(methods):
        raise ValueError('请选择有效且不重复的预测方法')
    return methods
