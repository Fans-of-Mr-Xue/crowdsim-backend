"""Explicit, separate macOS administrator-only powermetrics capture.

Never invoked by the simulation service; accepts only a diagnostic-enabled run.
The raw platform-specific output is retained instead of guessing unavailable
frequency/temperature fields. Does not elevate itself or launch the backend.
"""
import argparse
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import time


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=600, help='wall seconds to sample (maximum 3600)')
    parser.add_argument('--interval', type=float, default=5, help='wall seconds per sample (minimum 5)')
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval < 5 or args.interval > 3600:
        parser.error('--interval must be between 5 and 3600 seconds')
    if not math.isfinite(args.duration) or not 0 < args.duration <= 3600:
        parser.error('--duration must be between 0 and 3600 seconds')
    return args


def collect(args):
    directory = args.run_dir.resolve()
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('performance_measurement', {}).get('enabled') is not True:
        raise ValueError('The run must have been started with --monitor-performance')
    if platform.system() != 'Darwin':
        raise ValueError('powermetrics capture is supported only on macOS')
    if os.geteuid() != 0:
        raise PermissionError('powermetrics requires administrator privileges; run this helper separately with sudo')
    # Exclusive files protect prior captures and prevent following output symlinks.
    output = directory / 'performance.power.txt'
    metadata_path = directory / 'performance.power.metadata.json'
    command = ['/usr/bin/powermetrics', '--samplers', 'cpu_power,gpu_power,thermal',
               '--format', 'text', '--sample-rate', str(round(args.interval * 1000)),
               '--sample-count', str(math.ceil(args.duration / args.interval)), '--buffer-size', '1']
    with output.open('x', encoding='utf-8') as handle, metadata_path.open('x', encoding='utf-8') as meta:
        metadata = {'version': 1, 'run_id': manifest['run_id'], 'source': 'powermetrics',
                    'started_wall_time_unix': time.time(), 'interval_wall_seconds': args.interval,
                    'requested_duration_wall_seconds': args.duration, 'command': command,
                    'scope': 'whole_machine', 'status': 'running', 'output': output.name,
                    'fields': 'Raw fields depend on hardware and OS; absent fields are unavailable'}
        meta.write(json.dumps(metadata, indent=2) + '\n')
        meta.flush()
        try:
            result = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, text=True,
                                    timeout=math.ceil(args.duration / args.interval) * args.interval + 30)
            metadata.update(status='complete' if result.returncode == 0 else 'failed',
                            returncode=result.returncode, stderr=result.stderr[:4000])
        except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            metadata.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                            error=type(exc).__name__)
        finally:
            metadata['finished_wall_time_unix'] = time.time()
            meta.seek(0)
            meta.truncate()
            meta.write(json.dumps(metadata, indent=2) + '\n')
    return metadata


def main(argv=None):
    try:
        metadata = collect(parse_args(argv))
    except (OSError, ValueError, KeyError) as exc:
        print(f'Power capture failed: {exc}')
        return 1
    print(json.dumps(metadata, indent=2))
    return 0 if metadata['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
