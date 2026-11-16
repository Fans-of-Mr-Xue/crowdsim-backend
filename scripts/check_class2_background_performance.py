"""Headless class-two timings and invariants; no API/frontend service is started."""
import argparse
from collections import defaultdict
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crowdsim_overlay_server import build_requirement_runtime, parse_args
import crowdsim.core.simulation_runtime as runtime_module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requirement', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=100)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    record = json.loads(args.requirement.read_text())
    timings = defaultdict(lambda: {'calls': 0, 'seconds': 0., 'maxSeconds': 0.})
    def measure(obj, name, label):
        original = getattr(obj, name)
        def wrapped(*a, **kw):
            start = time.perf_counter()
            try: return original(*a, **kw)
            finally:
                elapsed = time.perf_counter()-start
                stats = timings[label]; stats['calls'] += 1
                stats['seconds'] += elapsed
                stats['maxSeconds'] = max(stats['maxSeconds'], elapsed)
        setattr(obj, name, wrapped)
    with tempfile.TemporaryDirectory(prefix='class2-background-benchmark-') as folder:
        directory = Path(folder)
        source = ROOT/'runs/artificial_societies/class2'
        target = directory/'runs/artificial_societies/class2'; target.mkdir(parents=True)
        index = record['requirement_id']+'.json'
        model = json.loads((source/index).read_text())['model_id']+'.json'
        for filename in (index, model): shutil.copy2(source/filename, target/filename)
        with patch.dict(os.environ, {'CROWDSIM_RUNS_ROOT': str(directory/'runs')}):
            started = time.perf_counter()
            runtime = build_requirement_runtime(parse_args(['--scenario', 'hotspot']), record)
            try:
                with patch.object(runtime_module, 'PROJECT_ROOT', directory):
                    runtime.initialize()
                init_seconds = time.perf_counter()-started
                runtime.init_frame()
                ids = set(runtime.current.persons)
                profiles = deepcopy(runtime.population.profiles)
                flows = runtime.class2_flows
                for obj, name, label in [
                    (flows, '_resume_background', 'background_trip'),
                    (flows, 'before_step', 'flow_control'),
                    (runtime.adapter, 'set_person_speed', 'speed_command'),
                    (runtime.adapter, 'step', 'sumo_step_and_snapshot'),
                    (runtime, '_prepare_boundary', 'boundary'),
                    (runtime.environment, 'observe', 'neighbour_observation'),
                    (runtime.state_updater, 'update_all', 'state_update'),
                    (runtime.recorder, 'record_step', 'recording'),
                    (runtime.route_provider.position_router, 'route', 'position_route'),
                ]: measure(obj, name, label)
                # Verify every background departure respects the full five-second hold.
                resume = flows._resume_background
                cycles = []
                def checked_resume(rt, pid, motion):
                    deadline = flows.background_departures[pid]
                    assert rt.time_seconds >= deadline
                    assert rt.time_seconds - deadline < rt.step_length + 1e-8
                    result = resume(rt, pid, motion)
                    cycles.append({'personId': pid, 'time': rt.time_seconds})
                    return result
                flows._resume_background = checked_resume
                runtime.start()
                started = time.perf_counter(); cpu_started = time.process_time()
                steps = []; last_report = 0
                while runtime.time_seconds < args.seconds:
                    before = runtime.time_seconds; stamp = time.perf_counter()
                    runtime.tick()
                    frame = runtime.frame()
                    json.dumps(frame)  # Include the normal update serialization cost.
                    steps.append({'from': before, 'seconds': time.perf_counter()-stamp})
                    assert set(runtime.current.persons) == ids
                    assert len(frame['pedestrians']) == len(ids)
                    for person in frame['pedestrians']:
                        for dimension in ('age_band', 'gender', 'origin', 'crowd_role'):
                            assert person['state'][dimension] == getattr(profiles[person['id']], dimension)
                    if runtime.time_seconds >= last_report+10:
                        last_report = runtime.time_seconds
                        print(json.dumps({'simulationSeconds': runtime.time_seconds,
                                          'wallSeconds': round(time.perf_counter()-started, 3)}), flush=True)
                wall_seconds = time.perf_counter()-started
                ordered = sorted(row['seconds'] for row in steps)
                result = {
                    'simulationSeconds': runtime.time_seconds, 'count': len(ids),
                    'backgroundCount': len(flows.background_ids),
                    'initializationSeconds': init_seconds, 'wallSeconds': wall_seconds,
                    'pythonCpuSeconds': time.process_time()-cpu_started,
                    'stepMeanSeconds': sum(ordered)/len(ordered),
                    'stepP95Seconds': ordered[int(.95*(len(ordered)-1))],
                    'stepMaxSeconds': max(ordered),
                    'slowestSteps': sorted(steps, key=lambda row: -row['seconds'])[:10],
                    'backgroundCycles': len(cycles), 'holdSeconds': 5,
                    'conservationError': runtime.population.diagnostics()['conservation_error'],
                    'identitiesPreserved': runtime.population.profiles == profiles,
                    'routing': runtime.route_provider.position_router.diagnostics(),
                    'timings': dict(timings),
                }
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
                print(json.dumps(result, ensure_ascii=False), flush=True)
            finally: runtime.close()


if __name__ == '__main__':
    main()
