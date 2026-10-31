"""Bounded, aggregate-only diagnostics. Never influence simulation decisions."""
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import json
import math
from pathlib import Path
import re
import time


@dataclass(frozen=True)
class PerformanceOptions:
    enabled: bool = False
    system_enabled: bool = True
    interval: float = 5.0

    def __post_init__(self):
        if not math.isfinite(self.interval) or self.interval < 5:
            raise ValueError('performance interval must be at least 5 seconds')


def add_performance_arguments(parser):
    parser.add_argument('--monitor-performance', action='store_true',
                        help='opt in to backend, frontend and host performance diagnostics')
    parser.add_argument('--performance-interval', type=float, default=5.0,
                        help='diagnostic aggregation/host sampling interval in seconds (minimum 5)')
    parser.add_argument('--performance-no-system', action='store_true',
                        help='with --monitor-performance, omit host sampling to measure its overhead')


def performance_options(args):
    enabled = bool(getattr(args, 'monitor_performance', False))
    interval = getattr(args, 'performance_interval', 5.0)
    no_system = bool(getattr(args, 'performance_no_system', False))
    if not enabled and (no_system or interval != 5.0):
        raise ValueError('performance options require --monitor-performance')
    return PerformanceOptions(enabled, not no_system, interval)


def timed(name):
    def decorate(function):
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            probe = getattr(self, 'performance', None)
            if probe is None or not probe.enabled:
                return function(self, *args, **kwargs)
            with probe.measure(name):
                return function(self, *args, **kwargs)
        return wrapped
    return decorate


class PerformanceProbe:
    def __init__(self, run_id, *, enabled=False, system_enabled=True, interval=5.0,
                 clock=time.perf_counter, cpu_clock=time.thread_time):
        self.run_id = run_id
        self.enabled = bool(enabled)
        self.system_enabled = bool(system_enabled)
        self.interval = interval
        self.clock = clock
        self.cpu_clock = cpu_clock
        self.started = clock() if self.enabled else 0.0
        self.directory = None
        self.error = None
        self.stats = {}
        self.cycles = 0
        self.running_wall = self.simulated = self.python_cpu = 0.0
        self.last_frontend = -math.inf
        self.frontend_final_saved = False
        self.system_monitor = None
        self.log_sizes = {}
        self.last_write = None

    def metadata(self):
        return {'enabled': self.enabled, 'interval_wall_seconds': self.interval, 'version': 2,
                'system_enabled': self.enabled and self.system_enabled,
                'activation': 'explicit_runtime_option', 'stage_cpu_scope': 'calling_thread',
                'timing_scopes_overlap': True}

    def start_system(self, runtime):
        if not self.enabled or not self.system_enabled or self.system_monitor is not None or self.directory is None:
            return
        from crowdsim.infrastructure.system_performance import SystemPerformanceMonitor

        def context():
            current = runtime.current
            return {'simulation_time': runtime.time_seconds, 'runtime_state': runtime.state.value,
                    'active_people': len(current.persons) if current else 0,
                    'requested_speed_factor': runtime.sim_speed_factor}

        self.system_monitor = SystemPerformanceMonitor(
            self.run_id, self.directory, context,
            enabled=True, interval=self.interval,
        )
        self.system_monitor.start()

    def close_system(self):
        if self.system_monitor is not None:
            self.system_monitor.close()

    def system_metadata(self):
        return self.system_monitor.metadata() if self.system_monitor else {'enabled': False, 'running': False}

    def attach(self, directory):
        if self.enabled:
            self.directory = Path(directory)

    def sample(self, name, value, unit='ms'):
        if not self.enabled or not math.isfinite(value) or value < 0:
            return
        row = self.stats.setdefault(name, {'count': 0, 'total': 0.0, 'max': 0.0, 'unit': unit})
        row['count'] += 1
        row['total'] += value
        row['max'] = max(row['max'], value)

    @contextmanager
    def measure(self, name, *, cpu=True):
        if not self.enabled:
            yield
            return
        started = self.clock()
        cpu_started = self.cpu_clock() if cpu else None
        try:
            yield
        finally:
            if self.enabled:
                self.sample(name, max(0.0, (self.clock() - started) * 1000))
                if cpu_started is not None:
                    value = max(0.0, (self.cpu_clock() - cpu_started) * 1000)
                    row = self.stats[name]
                    row['cpu_count'] = row.get('cpu_count', 0) + 1
                    row['cpu_total'] = row.get('cpu_total', 0.0) + value
                    row['cpu_max'] = max(row.get('cpu_max', 0.0), value)

    @contextmanager
    def offline_cycle(self, runtime):
        if not self.enabled:
            yield
            return
        started, cpu_started, simulated = self.clock(), time.process_time(), runtime.time_seconds
        try:
            with self.measure('tick_total'):
                yield
        finally:
            wall = self.clock() - started
            self.sample('cycle_total', wall * 1000)
            self.cycle(runtime.time_seconds - simulated, wall, time.process_time() - cpu_started)
            self.flush(runtime, force=runtime.state.value != 'RUNNING')

    def _logs(self):
        current, files = {}, {}
        try:
            for path in self.directory.iterdir():
                if path.suffix not in ('.csv', '.jsonl', '.log') or path.name.startswith('performance.'):
                    continue
                size = path.stat().st_size
                previous = self.log_sizes.get(path.name)
                files[path.name] = {'size_bytes': size, 'growth_bytes': size - previous
                                    if previous is not None and size >= previous else None}
                current[path.name] = size
            self.log_sizes = current
            return {'status': 'ok', 'scope': 'visible_file_sizes_not_physical_disk_io', 'files': files}
        except OSError as exc:
            return {'status': 'unavailable', 'reason': type(exc).__name__}

    def cycle(self, simulated, wall, cpu):
        if self.enabled:
            self.cycles += 1
            self.simulated += max(0.0, simulated)
            self.running_wall += max(0.0, wall)
            self.python_cpu += max(0.0, cpu)

    def _append(self, filename, payload):
        if not self.enabled or self.directory is None:
            return False
        started, cpu_started = self.clock(), self.cpu_clock()
        try:
            with (self.directory / filename).open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(payload, ensure_ascii=True, allow_nan=False) + '\n')
            return True
        except (OSError, ValueError) as exc:
            # A diagnostic write failure must not abort a scientific run.
            self.error = str(exc)
            self.enabled = False
            return False
        finally:
            self.last_write = {'file': filename, 'wall_ms': (self.clock() - started) * 1000,
                               'thread_cpu_ms': (self.cpu_clock() - cpu_started) * 1000}

    def flush(self, runtime, *, force=False):
        if not self.enabled or self.directory is None:
            return
        now = self.clock()
        if not force and now - self.started < self.interval:
            return
        if not self.stats and not self.cycles:
            self.started = now
            return
        payload = {
            'version': 2, 'run_id': self.run_id, 'wall_time_unix': time.time(),
            'window_wall_seconds': now - self.started,
            'simulation_time': runtime.time_seconds, 'runtime_state': runtime.state.value,
            'active_people': len(runtime.current.persons) if runtime.current else 0,
            'requested_speed_factor': runtime.sim_speed_factor, 'cycles': self.cycles,
            'running_cycle_wall_seconds': self.running_wall, 'simulated_seconds': self.simulated,
            'actual_speed_factor': self.simulated / self.running_wall if self.running_wall else None,
            'python_process_cpu_seconds': self.python_cpu,
            'python_cpu_one_core_percent': 100 * self.python_cpu / self.running_wall if self.running_wall else None,
            'timing_scopes_overlap': True, 'stage_cpu_scope': 'calling_thread',
            'stages': {key: {**row, 'mean': row['total'] / row['count'],
                            **({'cpu_mean': row['cpu_total'] / row['cpu_count']} if row.get('cpu_count') else {})}
                       for key, row in self.stats.items()},
            'log_files': self._logs(),
            'diagnostic_previous_write': self.last_write,
            'decision_log': runtime.recorder.decision_log_diagnostics if getattr(runtime, 'recorder', None) is not None else None,
        }
        self._append('performance.backend.jsonl', payload)
        self.stats.clear()
        self.cycles = 0
        self.running_wall = self.simulated = self.python_cpu = 0.0
        self.started = now

    def frontend(self, report):
        if not self.enabled or not isinstance(report, dict) or report.get('run_id') != self.run_id:
            return False
        now = self.clock()
        final = report.get('final') is True and not self.frontend_final_saved
        if now - self.last_frontend < 1.0 and not final:
            return False
        # Accept only small numeric aggregates from the current run, never paths
        # or arbitrary frame contents. Extra strings/objects are not persisted.
        stages = report.get('stages', {})
        if not isinstance(stages, dict) or len(stages) > 32:
            return False
        clean = {}
        for name, row in stages.items():
            if not isinstance(name, str) or not re.fullmatch(r'[a-z_]{1,48}', name) or not isinstance(row, dict):
                return False
            values = {key: row.get(key) for key in ('count', 'total', 'max', 'mean')}
            if any(type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1e12 for value in values.values()):
                return False
            if row.get('unit') not in ('ms', 'chars', 'people'):
                return False
            clean[name] = {**values, 'unit': row['unit']}
        data = {'version': 1, 'run_id': self.run_id, 'received_wall_time_unix': time.time(), 'stages': clean, 'final': final}
        for key in ('window_wall_seconds', 'simulation_time', 'requested_speed_factor', 'active_people',
                    'canvas_updates_per_second', 'map_updates_per_second', 'js_heap_used_bytes'):
            value = report.get(key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12):
                return False
            data[key] = value
        for key in ('visible', 'long_tasks_supported'):
            data[key] = report.get(key) if type(report.get(key)) is bool else None
        data['runtime_state'] = report.get('runtime_state') if report.get('runtime_state') in ('READY', 'RUNNING', 'PAUSED', 'FINISHED', 'ERROR', 'CLOSED') else None
        self.last_frontend = now
        saved = self._append('performance.frontend.jsonl', data)
        if saved and final:
            self.frontend_final_saved = True
        return saved
