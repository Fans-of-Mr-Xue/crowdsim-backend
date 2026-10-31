"""macOS read-only Mach/sysctl/libproc counters, without subprocess polling.

Layouts follow the local macOS SDK vm_statistics64, xsw_usage and
rusage_info_v2 definitions. Missing privileges/sources stay explicit.
"""
import ctypes as c
import os
import time


class VMStatistics64(c.Structure):
    _fields_ = (
        [(name, c.c_uint32) for name in ('free_count', 'active_count', 'inactive_count', 'wire_count')]
        + [(name, c.c_uint64) for name in ('zero_fill_count', 'reactivations', 'pageins', 'pageouts',
                                        'faults', 'cow_faults', 'lookups', 'hits', 'purges')]
        + [(name, c.c_uint32) for name in ('purgeable_count', 'speculative_count')]
        + [(name, c.c_uint64) for name in ('decompressions', 'compressions', 'swapins', 'swapouts')]
        + [(name, c.c_uint32) for name in ('compressor_page_count', 'throttled_count',
                                        'external_page_count', 'internal_page_count')]
        + [('total_uncompressed_pages_in_compressor', c.c_uint64), ('swapped_count', c.c_uint64)]
    )


class SwapUsage(c.Structure):
    _fields_ = [('total', c.c_uint64), ('available', c.c_uint64), ('used', c.c_uint64),
                ('page_size', c.c_uint32), ('encrypted', c.c_int32)]


class RUsageInfoV2(c.Structure):
    _fields_ = [('uuid', c.c_uint8 * 16)] + [(name, c.c_uint64) for name in (
        'user_time', 'system_time', 'pkg_idle_wkups', 'interrupt_wkups', 'pageins',
        'wired_size', 'resident_size', 'phys_footprint', 'proc_start_abstime', 'proc_exit_abstime',
        'child_user_time', 'child_system_time', 'child_pkg_idle_wkups', 'child_interrupt_wkups',
        'child_pageins', 'child_elapsed_abstime', 'diskio_bytesread', 'diskio_byteswritten')]


def counter_rates(current, previous, elapsed):
    if previous is None or elapsed is None or elapsed <= 0:
        return {'status': 'warming_up', 'per_second': {key: None for key in current}}
    reset = any(current[key] < previous[key] for key in current)
    return {'status': 'counter_reset' if reset else 'ok',
            'per_second': {key: None if reset else (value - previous[key]) / elapsed
                           for key, value in current.items()}}


class NativeMemorySampler:
    def __init__(self):
        self.lib = self.proc = None
        self.previous_vm = self.previous_vm_time = None
        self.previous_io = {}

    def _library(self):
        if self.lib is None:
            self.lib = c.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
            self.lib.mach_host_self.restype = c.c_uint
            self.lib.host_statistics64.argtypes = [c.c_uint, c.c_int, c.c_void_p, c.POINTER(c.c_uint)]
            self.lib.mach_port_deallocate.argtypes = [c.c_uint, c.c_uint]
            self.lib.sysctlbyname.argtypes = [c.c_char_p, c.c_void_p, c.POINTER(c.c_size_t), c.c_void_p, c.c_size_t]
        return self.lib

    def _sysctl(self, name, struct_type):
        lib = self._library()
        value, size = struct_type(), c.c_size_t(c.sizeof(struct_type))
        if lib.sysctlbyname(name.encode('ascii'), c.byref(value), c.byref(size), None, 0) != 0:
            raise OSError(c.get_errno(), 'sysctl read failed')
        if size.value != c.sizeof(value):
            raise OSError('unexpected sysctl layout')
        return value

    def _vm(self):
        lib = self._library()
        value, count = VMStatistics64(), c.c_uint(c.sizeof(VMStatistics64) // 4)
        host = lib.mach_host_self()
        try:
            if lib.host_statistics64(host, 4, c.byref(value), c.byref(count)) != 0:
                raise OSError('host_statistics64 failed')
            # macOS 26 adds swapped_count at the tail. Older kernels return
            # the 152-byte prefix; allocate the latest layout but accept that
            # prefix without inventing a value for the new field.
            if count.value < 38:
                raise OSError('incomplete VM statistics layout')
            value.available_words = count.value
        finally:
            lib.mach_port_deallocate(c.c_uint.in_dll(lib, 'mach_task_self_').value, host)
        return value

    def memory(self):
        try:
            vm = self._vm()
        except Exception:
            self.previous_vm = self.previous_vm_time = None
            raise
        now, page = time.monotonic(), os.sysconf('SC_PAGE_SIZE')
        elapsed = now - self.previous_vm_time if self.previous_vm_time is not None else None
        counters = {name: getattr(vm, name) for name in ('pageins', 'pageouts', 'swapins', 'swapouts',
                                                       'compressions', 'decompressions')}
        rates = counter_rates(counters, self.previous_vm, elapsed)
        self.previous_vm, self.previous_vm_time = counters, now
        result = {'status': 'ok', 'source': 'host_statistics64+sysctl', 'scope': 'whole_machine',
                  'page_size_bytes': page, 'window_wall_seconds': elapsed,
                  'vm_layout_words': getattr(vm, 'available_words', 40),
                  'page_counters': counters, 'page_rates': rates,
                  'swap_io_bytes_per_second': {name: value * page if value is not None else None
                                              for name, value in rates['per_second'].items()
                                              if name in ('swapins', 'swapouts')},
                  'memory_bytes': {name: getattr(vm, field) * page for name, field in (
                      ('free', 'free_count'), ('active', 'active_count'), ('inactive', 'inactive_count'),
                      ('wired', 'wire_count'), ('compressor_physical', 'compressor_page_count'),
                      ('compressor_uncompressed', 'total_uncompressed_pages_in_compressor'))}}
        result['memory_bytes']['swapped_logical'] = vm.swapped_count * page if getattr(vm, 'available_words', 40) >= 40 else None
        for name, key, struct_type in (
            ('total_memory', 'hw.memsize', c.c_uint64),
            ('swap', 'vm.swapusage', SwapUsage),
            ('pressure', 'kern.memorystatus_vm_pressure_level', c.c_uint32),
        ):
            try:
                value = self._sysctl(key, struct_type)
                if name == 'swap':
                    result[name] = {'status': 'ok', 'used_bytes': value.used, 'total_bytes': value.total,
                                    'available_bytes': value.available}
                else:
                    result[name] = {'status': 'ok', 'bytes' if name == 'total_memory' else 'raw_level': value.value}
            except Exception as exc:
                result['status'] = 'partial'
                result[name] = {'status': 'unavailable', 'reason': type(exc).__name__}
        return result

    def _usage(self, pid):
        if self.proc is None:
            self.proc = c.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            self.proc.proc_pid_rusage.argtypes = [c.c_int, c.c_int, c.c_void_p]
        usage = RUsageInfoV2()
        if self.proc.proc_pid_rusage(pid, 2, c.byref(usage)) != 0:
            raise OSError(c.get_errno(), 'proc_pid_rusage failed')
        return usage

    def process_io(self, processes):
        result, current = [], {}
        for pid, row in processes.items():
            if row['group'] not in ('backend', 'sumo_children'):
                continue
            try:
                usage = self._usage(pid)
                now = time.monotonic()
                counters = {'disk_read_bytes': usage.diskio_bytesread, 'disk_write_bytes': usage.diskio_byteswritten,
                            'pageins': usage.pageins}
                identity = (row['identity'], usage.proc_start_abstime)
                old = self.previous_io.get(pid)
                elapsed = now - old['time'] if old else None
                reused = old is not None and old['identity'] != identity
                rates = counter_rates(counters, old['counters'] if old and not reused else None, elapsed)
                if reused:
                    rates['status'] = 'pid_reused'
                result.append({'pid': pid, 'group': row['group'], 'status': 'ok',
                               'window_wall_seconds': elapsed, 'counters': counters, 'rates': rates,
                               'resident_bytes': usage.resident_size, 'physical_footprint_bytes': usage.phys_footprint})
                current[pid] = {'time': now, 'identity': identity, 'counters': counters}
            except Exception as exc:
                result.append({'pid': pid, 'group': row['group'], 'status': 'unavailable', 'reason': type(exc).__name__})
        self.previous_io = current
        return {'status': 'not_found' if not result else 'ok' if all(row['status'] == 'ok' for row in result) else 'partial',
                'source': 'proc_pid_rusage.RUSAGE_INFO_V2', 'scope': 'backend_and_owned_sumo', 'processes': result}
