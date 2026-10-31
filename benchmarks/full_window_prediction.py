"""Manual native-engine load test; all demand values are explicit assumptions.

PYTHONPATH=. .venv-backend/bin/python benchmarks/full_window_prediction.py --minutes 270
"""
import argparse
import json
import time
from datetime import datetime, timedelta

from data_service.prediction.demo import monument_inputs
from data_service.prediction.inputs import normalize_settings
from data_service.prediction.runtime_budget import runtime_budget
from data_service.prediction.solver import compute_comparison


def main():
    parser = argparse.ArgumentParser(description='纪念塔完整时间窗性能验证（假设客流，非现场观测）')
    parser.add_argument('--minutes', type=int, default=270)
    parser.add_argument('--initial', type=int, default=150)
    parser.add_argument('--arrivals', type=float, default=1)
    parser.add_argument('--dwell', type=float, default=5)
    parser.add_argument('--methods', nargs='+', default=['conservation', 'collision_free_speed', 'social_force'])
    args = parser.parse_args()
    task, roads, settings = monument_inputs()
    start = datetime(2026, 12, 31, 20)
    task['time_window'] = f'{start:%Y-%m-%d %H:%M} 至 {start + timedelta(minutes=args.minutes):%Y-%m-%d %H:%M}'
    settings.update(initial_crowd=args.initial, arrival_rate_per_minute=args.arrivals,
                    arrival_schedule=[], dwell_minutes=args.dwell, grid_m=12, sample_minutes=5)
    settings = normalize_settings(task, settings)
    began = time.monotonic()
    result = compute_comparison(task, roads, settings, args.methods)
    report = dict(scenario='纪念塔 SUMO 路网；测试客流，不是观测', time_window=task['time_window'],
                  initial=args.initial, arrivals_per_minute=args.arrivals, dwell_minutes=args.dwell,
                  elapsed_seconds=round(time.monotonic() - began, 3), budget=runtime_budget(),
                  errors=[dict(method=row['method']['id'], message=row['message']) for row in result['method_errors']],
                  results=[dict(method=row['method']['id'], summary=row['summary'], diagnostics=row['diagnostics'],
                                first_minute=row['series'][0]['minute'], last_minute=row['series'][-1]['minute'],
                                samples=len(row['series'])) for row in result['results']])
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
