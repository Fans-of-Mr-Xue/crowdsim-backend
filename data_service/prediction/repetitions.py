"""Repeat independent runs without averaging positions into invented trajectories."""
import math

SUMMARY_METRICS = ('peak_crowd', 'peak_density_p_m2', 'max_outside_queue',
                   'departed', 'remaining_crowd', 'mean_queue_wait_minutes')


def repetition_seeds(settings):
    count = settings.get('repetitions', 1)
    if isinstance(count, bool) or not isinstance(count, (int, float)) or not 1 <= count <= 5 or count != int(count):
        raise ValueError('实验重复次数须为 1–5 的整数')
    # Wrap at the seed bound, preserving distinct seeds at the upper limit.
    return [(int(settings['seed']) + index) % 2147483648 for index in range(int(count))]


def summarize_repetitions(runs):
    metrics = {}
    for key in SUMMARY_METRICS:
        values = [row['summary'].get(key) for row in runs]
        if all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
            metrics[key] = {'mean': sum(values) / len(values), 'min': min(values), 'max': max(values)}
    return {'count': len(runs), 'seeds': [row['diagnostics']['seed'] for row in runs], 'metrics': metrics}
