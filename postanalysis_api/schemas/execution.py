"""Numerical control settings embedded in the reviewed experiment design."""
from .common import ApiError


def validate_execution_settings(value, duration_seconds=86400):
    """Reviewed settings for the current memorial executor, shared by intake/template."""
    from .common import object_, integer
    from .experiments import _metric_ids, _pc_features
    data = object_(value, "plan.executionSettings", keys={"seedStart", "parallelism", "metricIds", "pcFeatures"})
    seed = integer(data.get("seedStart"), "plan.executionSettings.seedStart", 0, 2147483647)
    parallelism = integer(data.get("parallelism"), "plan.executionSettings.parallelism", 1, 1)
    metric_ids = _metric_ids(data.get("metricIds"), "plan.executionSettings.metricIds")
    features = _pc_features(data.get("pcFeatures"), duration_seconds)
    if len(features) > 8 or any(feature["metricId"] not in {"peak_density", "mean_speed", "peak_pressure_proxy"}
                              or feature["metricId"] not in metric_ids or feature["regionId"] not in {"global", "A", "B", "C"}
                              for feature in features):
        raise ApiError("EXECUTION_SETTINGS_INVALID", "PC须使用2—8个已选密度、速度或压力代理特征，区域为global/A/B/C", 422)
    return {"seedStart": seed, "parallelism": parallelism, "metricIds": metric_ids, "pcFeatures": features}


def validate_group_execution(value, duration_seconds=86400):
    if isinstance(value, dict) and 'actions' in value:
        from .common import array, text_, integer, object_
        from postanalysis_api.workers.native_runtime import native_action
        object_(value, 'execution', keys={'actions', 'referenceDesignId'})
        text_(value.get('referenceDesignId'), 'execution.referenceDesignId', max_len=64)
        seen = set()
        previous = 0
        for action in array(value.get('actions'), 'execution.actions', 0, 10):
            object_(action, 'execution.actions[]', keys={'id', 'actionType', 'atSeconds', 'target', 'parameters', 'targetRegionId'})
            text_(action.get('id'), 'action.id', max_len=64, pattern=r'[A-Za-z0-9][A-Za-z0-9_-]*')
            at = integer(action.get('atSeconds'), 'action.atSeconds', 1, duration_seconds - 1)
            if action['id'] in seen or at < previous:
                raise ApiError('EXECUTION_DESIGN_INVALID', '动作编号须唯一且按执行时间排序', 422)
            native_action(action)
            seen.add(action['id']); previous = at
        return value
    keys = {"startSeconds", "compliance", "delaySeconds", "referenceDesignId"}
    valid = isinstance(value, dict) and set(value) == keys
    if valid:
        valid = (type(value["startSeconds"]) is int and 1 <= value["startSeconds"] < duration_seconds
                 and type(value["delaySeconds"]) is int and 0 <= value["delaySeconds"] <= duration_seconds
                 and type(value["compliance"]) in (int, float) and 0 <= value["compliance"] <= 1
                 and isinstance(value["referenceDesignId"], str) and 1 <= len(value["referenceDesignId"]) <= 64)
    if not valid:
        raise ApiError("EXECUTION_DESIGN_INVALID", "工程设计必须明确有效的启动秒数、遵从比例、响应延迟和参照设计组", 422)
    return value
