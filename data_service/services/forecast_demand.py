"""Resolve domain-derived arrivals at the experiment boundary, never on old results."""
import copy

from ..prediction.inputs import default_settings, normalize_settings
from .domain_demand import effective_estimate


def resolve_forecast_inputs(task, overrides=None, *, strict=False):
    components = task.get('components', {})
    portrait = components.get('domain_portrait', {})
    configured = any(portrait.get(k) is not None for k in ('demand_inputs', 'gate_counts'))
    mode = components.get('forecast_demand_source', {}).get('mode', 'domain' if configured else 'manual')
    if mode not in ('domain', 'manual'):
        raise ValueError('客流来源须为领域模型或手动设置')
    settings = default_settings(task)
    if overrides is not None and (not isinstance(overrides, dict) or set(overrides) - set(settings)):
        raise ValueError('预测配置包含不支持的字段')
    settings.update(copy.deepcopy(overrides or {}))
    origin = {'mode': mode, 'status': 'empty', 'task_revision': task.get('revision')}
    if configured:
        try:
            demand = effective_estimate(task, portrait)
            origin.update(status='ready', fingerprint=demand['fingerprint'], method=demand['method'],
                          total_visits=demand['total_visits'], baseline_visits=demand['baseline_visits'],
                          activity_decisions=demand['activity_decisions'], forecast_inputs=copy.deepcopy(demand['forecast_inputs']))
            if demand.get('gate_counts'):
                origin['gate_counts'] = copy.deepcopy(demand['gate_counts'])
        except (ValueError, TypeError, KeyError) as exc:
            origin.update(status='stale', message=str(exc))
    if mode == 'domain':
        if origin['status'] == 'ready':
            # Domain totals are NEW arrivals, never initial occupancy or peak crowd.
            if 'gate_counts' in origin and settings['exit_points'] != portrait['gate_counts']['scope']['exit_points']:
                raise ValueError('实验入口位置与计数表不一致，请重新核对入口')
            settings.update(origin['forecast_inputs'])
        elif strict:
            raise ValueError('领域客流尚未就绪或范围已变化，请在领域模型核对后重试，或明确切换手动客流。')
    if strict:
        # Validate the effective curve, not an obsolete client curve it replaces.
        settings = normalize_settings(task, settings)
    origin.update(initial_crowd=settings.get('initial_crowd'), dwell_minutes=settings.get('dwell_minutes'))
    return settings, origin
