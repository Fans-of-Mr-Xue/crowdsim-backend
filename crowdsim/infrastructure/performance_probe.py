"""Bounded, aggregate-only diagnostics. Never influence simulation decisions."""
from contextlib import contextmanager
from functools import wraps
import json
import math
import os
from pathlib import Path
import re
import time


def timed(name):
    def decorate(function):
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            with self.performance.measure(name):
                return function(self, *args, **kwargs)
        return wrapped
    return decorate


class PerformanceProbe:
    def __init__(self, run_id, *, enabled=None, interval=5.0, clock=time.perf_counter):
        self.run_id = run_id
        self.enabled = os.environ.get('CROWDSIM_PERF', '1') != '0' if enabled is None else enabled
        self.interval = interval
        self.clock = clock
        self.started = clock()
        self.directory = None
        self.error = None
        self.stats = {}
        self.cycles = 0
        self.running_wall = self.simulated = self.python_cpu = 0.0
        self.last_frontend = -math.inf
        self.frontend_final_saved = False
        self.system_monitor = None

    def start_system(self, runtime):
        if self.system_monitor is not None or self.directory is None:
            return
        from crowdsim.infrastructure.system_performance import SystemPerformanceMonitor

        def context():
            current = runtime.current
            return {'simulation_time': runtime.time_seconds, 'runtime_state': runtime.state.value,
                    'active_people': len(current.persons) if current else 0,
                    'requested_speed_factor': runtime.sim_speed_factor}

        self.system_monitor = SystemPerformanceMonitor(
            self.run_id, self.directory, context,
            enabled=self.enabled and os.environ.get('CROWDSIM_SYSTEM_PERF', '1') != '0',
        )
        self.system_monitor.start()

    def close_system(self):
        if self.system_monitor is not None:
            self.system_monitor.close()

    def system_metadata(self):
        return self.system_monitor.metadata() if self.system_monitor else {'enabled': False, 'running': False}

    def attach(self, directory):
        self.directory = Path(directory)

    def sample(self, name, value, unit='ms'):
        if not self.enabled or not math.isfinite(value) or value < 0:
            return
        row = self.stats.setdefault(name, {'count': 0, 'total': 0.0, 'max': 0.0, 'unit': unit})
        row['count'] += 1
        row['total'] += value
        row['max'] = max(row['max'], value)

    @contextmanager
    def measure(self, name):
        if not self.enabled:
            yield
            return
        started = self.clock()
        try:
            yield
        finally:
            self.sample(name, (self.clock() - started) * 1000)

    def cycle(self, simulated, wall, cpu):
        if self.enabled:
            self.cycles += 1
            self.simulated += max(0.0, simulated)
            self.running_wall += max(0.0, wall)
            self.python_cpu += max(0.0, cpu)

    def _append(self, filename, payload):
        if not self.enabled or self.directory is None:
            return False
        try:
            with (self.directory / filename).open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(payload, ensure_ascii=True, allow_nan=False) + '\n')
            return True
        except (OSError, ValueError) as exc:
            # A diagnostic write failure must not abort a scientific run.
            self.error = str(exc)
            self.enabled = False
            return False

    def flush(self, runtime, *, force=False):
        now = self.clock()
        if not self.enabled or self.directory is None or (not force and now - self.started < self.interval):
            return
        if not self.stats and not self.cycles:
            self.started = now
            return
        payload = {
            'version': 1, 'run_id': self.run_id, 'wall_time_unix': time.time(),
            'window_wall_seconds': now - self.started,
            'simulation_time': runtime.time_seconds, 'runtime_state': runtime.state.value,
            'active_people': len(runtime.current.persons) if runtime.current else 0,
            'requested_speed_factor': runtime.sim_speed_factor, 'cycles': self.cycles,
            'running_cycle_wall_seconds': self.running_wall, 'simulated_seconds': self.simulated,
            'actual_speed_factor': self.simulated / self.running_wall if self.running_wall else None,
            'python_process_cpu_seconds': self.python_cpu,
            'python_cpu_one_core_percent': 100 * self.python_cpu / self.running_wall if self.running_wall else None,
            'stages': {key: {**row, 'mean': row['total'] / row['count']} for key, row in self.stats.items()},
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
