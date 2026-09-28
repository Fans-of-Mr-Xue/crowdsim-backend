"""Low-frequency, read-only host diagnostics. No TraCI calls or extra packages.

macOS driver statistics are best-effort snapshots, not stable public APIs.
Unavailable fields are null with a reason, never fabricated zeroes.
"""
import ctypes
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import threading
import time


def command(args, *, fixed_locale=False):
    # Local to this child; never change the backend/SUMO process environment.
    env = {**os.environ, 'LC_ALL': 'C', 'LANG': 'C'} if fixed_locale else None
    return subprocess.run(args, capture_output=True, check=True, timeout=1.5, env=env).stdout


def finite(value, low=0, high=math.inf):
    return value if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None


PROCESS_GROUPS = ('backend', 'sumo_children', 'browser_related_all_apps', 'window_server', 'editor_and_codex')
START_TIME = re.compile(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ([1-9]|[12][0-9]|3[01]) ([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9] [0-9]{4}')
CPU_TIME = re.compile(r'(?:(\d+)-)?(?:(\d+):)?(\d+):([0-5]\d(?:\.\d+)?)')


def parse_processes(data):
    result = {}
    total = failed = 0
    for line in data.decode('utf-8', errors='replace').splitlines():
        if not line.strip():
            continue
        total += 1
        # C-locale lstart has five columns. Validate rather than silently
        # treating a localized date or executable path as part of the date.
        parts = line.split(None, 9)
        if len(parts) != 10:
            failed += 1
            continue
        try:
            identity = ' '.join(parts[4:9])
            match = CPU_TIME.fullmatch(parts[2])
            if not START_TIME.fullmatch(identity) or match is None:
                raise ValueError('unexpected date or CPU time format')
            days, hours, minutes, seconds = match.groups()
            cpu = int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[3])
            if pid <= 0 or ppid < 0 or rss < 0 or not math.isfinite(cpu) or pid in result:
                raise ValueError('invalid process fields')
            result[pid] = {
                'ppid': ppid, 'cpu_seconds': cpu, 'rss_bytes': rss * 1024,
                'identity': identity, 'name': Path(parts[9]).name,
            }
        except (ValueError, OverflowError):
            failed += 1
    return result, {'raw_rows': total, 'parsed_rows': len(result), 'failed_rows': failed}


def process_group(pid, row, rows, backend_pid):
    if pid == backend_pid:
        return 'backend'
    name = row['name'].lower()
    if name in ('sumo', 'sumo-gui'):
        parent, visited = row['ppid'], set()
        while parent in rows and parent not in visited:
            if parent == backend_pid:
                return 'sumo_children'
            visited.add(parent)
            parent = rows[parent]['ppid']
    if any(part in name for part in ('webkit', 'safari', 'google chrome', 'chromium', 'firefox', 'microsoft edge')):
        return 'browser_related_all_apps'
    if name == 'windowserver':
        return 'window_server'
    if name.startswith(('cursor', 'codex')):
        return 'editor_and_codex'
    return None


class MacSampler:
    def __init__(self):
        self.previous_ticks = None
        self.previous_processes = {}
        self.previous_process_time = None
        self.backend_pid = os.getpid()
        self.lib = None
        self.objc = None

    def cpu(self):
        if self.lib is None:
            self.lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
            self.lib.mach_host_self.restype = ctypes.c_uint
            self.lib.host_statistics.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            self.lib.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        ticks = (ctypes.c_uint * 4)()
        count = ctypes.c_uint(4)
        host = self.lib.mach_host_self()
        try:
            if self.lib.host_statistics(host, 3, ticks, ctypes.byref(count)) != 0:
                raise OSError('host_statistics failed')
        finally:
            task = ctypes.c_uint.in_dll(self.lib, 'mach_task_self_').value
            self.lib.mach_port_deallocate(task, host)
        current = list(ticks)
        previous, self.previous_ticks = self.previous_ticks, current
        if previous is None:
            return {'status': 'warming_up', 'busy_percent': None}
        delta = [(new - old) % (2 ** 32) for new, old in zip(current, previous)]
        total = sum(delta)
        return {'status': 'ok' if total else 'warming_up',
                'busy_percent': 100 * (total - delta[2]) / total if total else None,
                'logical_cpu_count': os.cpu_count(), 'scope': 'whole_machine_all_cores'}

    def processes(self):
        try:
            rows, quality = parse_processes(command(
                ['/bin/ps', '-ww', '-axo', 'pid=,ppid=,time=,rss=,lstart=,comm='], fixed_locale=True,
            ))
        except Exception:
            # Do not join CPU differences across an unobserved interval.
            self.previous_processes = {}
            self.previous_process_time = None
            raise
        now = time.monotonic()
        elapsed = now - self.previous_process_time if self.previous_process_time is not None else None
        quality['backend_pid_found'] = self.backend_pid in rows
        status = 'unavailable' if not quality['backend_pid_found'] else 'partial' if quality['failed_rows'] else 'ok'
        quality['missing_groups'] = []
        groups = {key: [] for key in PROCESS_GROUPS}
        selected = {}
        for pid, row in rows.items():
            group = process_group(pid, row, rows, self.backend_pid)
            if group is None:
                continue
            old = self.previous_processes.get(pid)
            percent = None
            cpu_status = 'warming_up'
            if old and old['identity'] != row['identity']:
                cpu_status = 'pid_reused'
            elif old and row['cpu_seconds'] < old['cpu_seconds']:
                cpu_status = 'counter_reset'
            elif old and elapsed is not None and elapsed > 0:
                percent = 100 * (row['cpu_seconds'] - old['cpu_seconds']) / elapsed
                cpu_status = 'ok'
            if status == 'unavailable':
                percent, cpu_status = None, 'unavailable'
            selected[pid] = {**row, 'group': group}
            groups[group].append({
                'pid': pid, 'name': row['name'], 'cpu_one_core_percent': percent,
                'cpu_status': cpu_status, 'rss_bytes': row['rss_bytes'],
            })
        absent = {key: 0 for key in PROCESS_GROUPS}
        for pid, old in self.previous_processes.items():
            if pid not in selected or selected[pid]['identity'] != old['identity']:
                absent[old['group']] += 1
        aggregates = {}
        for group, items in groups.items():
            if not items:
                quality['missing_groups'].append(group)
            matched = len(items)
            valid = sum(item['cpu_one_core_percent'] is not None for item in items)
            snapshot_status = status if status != 'ok' else 'ok' if matched else 'not_found'
            cpu_complete = snapshot_status == 'ok' and valid == matched and not absent[group]
            known_cpu = sum(item['cpu_one_core_percent'] for item in items if item['cpu_one_core_percent'] is not None) if valid else None
            if cpu_complete:
                aggregate_cpu_status = 'ok'
            elif snapshot_status in ('unavailable', 'not_found'):
                aggregate_cpu_status = snapshot_status
            elif elapsed is None and status == 'ok':
                aggregate_cpu_status = 'warming_up'
            else:
                aggregate_cpu_status = 'partial'
            aggregates[group] = {
                'status': snapshot_status,
                'cpu_status': aggregate_cpu_status,
                'process_count': matched, 'cpu_measured_process_count': valid,
                'previous_processes_not_observed': absent[group],
                'cpu_one_core_percent': known_cpu if cpu_complete else None,
                'observed_cpu_one_core_percent': known_cpu,
                'rss_bytes_sum': sum(item['rss_bytes'] for item in items) if snapshot_status == 'ok' else None,
                'observed_rss_bytes_sum': sum(item['rss_bytes'] for item in items) if items else None,
                'processes': items,
            }
        # Store selected processes only; neither full command lines nor other apps are logged.
        self.previous_processes = selected if status != 'unavailable' else {}
        self.previous_process_time = now if status != 'unavailable' else None
        return {'status': status, 'reason': 'backend PID missing from process snapshot' if status == 'unavailable'
                else 'some process rows could not be parsed' if status == 'partial' else None,
                'quality': quality, 'window_wall_seconds': elapsed, 'groups': aggregates}

    def gpu(self):
        rows = plistlib.loads(command(['/usr/sbin/ioreg', '-r', '-c', 'AGXAccelerator', '-d', '1', '-a']))
        devices = []
        for row in rows:
            stats = row.get('PerformanceStatistics', {})
            devices.append({
                'model': row.get('model') if isinstance(row.get('model'), str) else None,
                'device_utilization_percent': finite(stats.get('Device Utilization %'), high=100),
                'renderer_utilization_percent': finite(stats.get('Renderer Utilization %'), high=100),
                'tiler_utilization_percent': finite(stats.get('Tiler Utilization %'), high=100),
                'in_use_system_memory_bytes': finite(stats.get('In use system memory')),
            })
        return {'status': 'ok' if any(d['device_utilization_percent'] is not None for d in devices) else 'unavailable',
                'reason': None if devices else 'AGX driver statistics not exposed',
                'source': 'ioreg.AGXAccelerator.PerformanceStatistics', 'scope': 'whole_machine', 'devices': devices}

    def thermal(self):
        if self.objc is None:
            self.foundation = ctypes.CDLL('/System/Library/Frameworks/Foundation.framework/Foundation')
            self.objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
            self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
            self.objc.objc_getClass.restype = ctypes.c_void_p
            self.objc.sel_registerName.argtypes = [ctypes.c_char_p]
            self.objc.sel_registerName.restype = ctypes.c_void_p
        selector = self.objc.sel_registerName
        send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', self.objc))
        get = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', self.objc))
        pool = send(self.objc.objc_getClass(b'NSAutoreleasePool'), selector(b'new'))
        try:
            process = send(self.objc.objc_getClass(b'NSProcessInfo'), selector(b'processInfo'))
            if not process:
                raise OSError('NSProcessInfo unavailable')
            level = get(process, selector(b'thermalState'))
            return {'status': 'ok' if level in range(4) else 'unavailable',
                    'pressure_level': level, 'pressure': {0: 'nominal', 1: 'fair', 2: 'serious', 3: 'critical'}.get(level),
                    'source': 'NSProcessInfo.thermalState'}
        finally:
            send(pool, selector(b'drain'))

    def battery(self):
        rows = plistlib.loads(command(['/usr/sbin/ioreg', '-r', '-c', 'AppleSmartBattery', '-d', '1', '-a']))
        row = rows[0] if rows else {}
        raw = finite(row.get('Temperature'), low=1, high=10000)
        return {'status': 'ok' if raw is not None else 'unavailable',
                'temperature_celsius': raw / 100 if raw is not None else None,
                'temperature_raw': raw, 'source': 'AppleSmartBattery.Temperature/100',
                'external_power_connected': row.get('ExternalConnected') if type(row.get('ExternalConnected')) is bool else None,
                'charging': row.get('IsCharging') if type(row.get('IsCharging')) is bool else None}

    def sample(self):
        result = {}
        for name in ('cpu', 'processes', 'gpu', 'thermal', 'battery'):
            try:
                result[name] = getattr(self, name)()
            except Exception as exc:
                result[name] = {'status': 'unavailable', 'reason': type(exc).__name__}
        result['chip_temperature'] = {
            'status': 'unavailable', 'cpu_celsius': None, 'gpu_celsius': None,
            'reason': 'No supported unprivileged chip temperature source configured; thermal pressure and battery temperature are not chip temperatures',
        }
        return result


class SystemPerformanceMonitor:
    def __init__(self, run_id, directory, context, *, enabled=True, interval=5.0, sampler=None):
        self.run_id = run_id
        self.directory = Path(directory)
        self.context = context
        self.enabled = enabled
        self.interval = max(5.0, interval)
        self.sampler = sampler
        self.error = None
        self.thread = None
        self.stop_event = threading.Event()

    def metadata(self):
        return {'enabled': self.enabled, 'platform': platform.system(), 'interval_wall_seconds': self.interval,
                'file': 'performance.system.jsonl', 'version': 1,
                'running': bool(self.thread and self.thread.is_alive()), 'error': self.error}

    def start(self):
        if not self.enabled or self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name='crowdsim-system-performance', daemon=True)
        try:
            self.thread.start()
        except RuntimeError as exc:
            self.thread = None
            self.error = str(exc)

    def _record(self, final=False):
        started = time.monotonic()
        context = self.context()
        if self.sampler is None:
            data = {'status': 'unavailable', 'reason': 'Only macOS host sampling is implemented'}
        else:
            data = self.sampler.sample()
        terminal = context.get('runtime_state') in ('FINISHED', 'ERROR', 'CLOSED')
        row = {'version': 1, 'run_id': self.run_id, 'wall_time_unix': time.time(),
               'sample_started_wall_time_unix': time.time() - (time.monotonic() - started),
               'interval_wall_seconds': self.interval, 'final': final or terminal, **context,
               'host': data, 'collection_wall_ms': (time.monotonic() - started) * 1000}
        with (self.directory / 'performance.system.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=True, allow_nan=False) + '\n')
        return terminal or self.sampler is None

    def _run(self):
        try:
            if self.sampler is None and platform.system() == 'Darwin':
                self.sampler = MacSampler()
            while True:
                started = time.monotonic()
                if self._record():
                    break
                if self.stop_event.wait(max(0, self.interval - (time.monotonic() - started))):
                    self._record(final=True)
                    break
        except Exception as exc:
            # Disk/diagnostics failure must not turn a successful run into ERROR.
            self.error = f'{type(exc).__name__}: {exc}'

    def close(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                self.error = 'monitor shutdown timeout'
