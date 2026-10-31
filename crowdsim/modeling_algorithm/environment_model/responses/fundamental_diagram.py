"""Configurable linear density-speed envelope applied over SUMO motion."""
import math

METHOD_NAME = '基本图模型'
MODULE_KIND = 'environment_response'
IMPLEMENTED = True


def execute(density, free_speed, *, critical_density=4.0, minimum_speed_ratio=0.1):
    """v_limit = v_free * max(r_min, 1 - density / critical_density).

    This is an engineering linear fundamental diagram, not a calibrated
    site-specific pedestrian model. Missing density produces no extra limit.
    """
    if not (math.isfinite(critical_density) and critical_density > 0
            and math.isfinite(minimum_speed_ratio) and 0 <= minimum_speed_ratio <= 1):
        raise ValueError('invalid fundamental diagram parameters')
    if density is None:
        return None
    if not math.isfinite(density) or density < 0 or not math.isfinite(free_speed) or free_speed < 0:
        raise ValueError('density and free speed must be nonnegative and finite')
    return free_speed * max(minimum_speed_ratio, 1.0 - density / critical_density)
