"""Real GLM + headless SUMO experiment; never starts API/frontend or edits sessions."""
import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from crowdsim_overlay_server import build_requirement_runtime, parse_args
import crowdsim.core.simulation_runtime as runtime_module


def audit_context(context):
    radius = context['perception']['radius_m']
    points = [person['relative_xy_m'] for person in context['nearby_people']]
    points += [point for road in context['local_roads'] for point in road['relative_path_m']]
    points += [option['waypoint_relative_xy_m'] for option in context['local_options']]
    assert all(math.hypot(*point) <= radius + .002 for point in points)
    assert all(set(person) == {'relative_xy_m', 'speed_mps', 'heading_degrees'} for person in context['nearby_people'])
    assert set(context['goal']) == {'direction_east_north', 'instruction'}


async def run(args):
    load_dotenv(ROOT / '.env', override=False)
    record = json.loads(args.requirement.read_text())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    trace_path = args.output.with_suffix('.decisions.jsonl')
    with tempfile.TemporaryDirectory(prefix='class2-local-llm-') as folder, trace_path.open('w') as trace:
        directory = Path(folder)
        source = ROOT / 'runs/artificial_societies/class2'
        target = directory / 'runs/artificial_societies/class2'
        target.mkdir(parents=True)
        index = record['requirement_id'] + '.json'
        model_file = json.loads((source / index).read_text())['model_id'] + '.json'
        for filename in (index, model_file):
            shutil.copy2(source / filename, target / filename)
        with patch.dict(os.environ, {'CROWDSIM_RUNS_ROOT': str(directory / 'runs')}):
            runtime = build_requirement_runtime(parse_args(['--scenario', 'hotspot']), record)
            runtime.archive_root = directory / 'runs'
            try:
                with patch.object(runtime_module, 'PROJECT_ROOT', directory):
                    runtime.initialize()
                # Exercise the actual production wiring, not a manually attached controller.
                controller = runtime.class2_local_pedestrians
                assert controller is not None
                model = controller.model
                if args.timeout is not None:
                    model.timeout_seconds = args.timeout
                if args.concurrency is not None:
                    controller.concurrency = args.concurrency
                profiles = deepcopy(runtime.population.profiles)
                ids = set(runtime.current.persons)
                initial_positions = {pid: (p.x, p.y) for pid, p in runtime.current.persons.items()}
                initial_distances = {pid: math.hypot(runtime.current.persons[pid].x-t['x'], runtime.current.persons[pid].y-t['y'])
                                     for pid, t in controller.final_targets.items()}
                runtime.start()
                cycles = []
                resume = runtime.class2_flows._resume_background
                def checked_resume(rt, pid, motion):
                    deadline = runtime.class2_flows.background_departures[pid]
                    assert deadline <= rt.time_seconds < deadline + rt.step_length + 1e-8
                    result = resume(rt, pid, motion)
                    cycles.append({'person_id': pid, 'time': rt.time_seconds})
                    return result
                runtime.class2_flows._resume_background = checked_resume
                started = time.perf_counter()
                round_seconds = 0.0
                steps = 0
                step_timings = []
                wait_checks = slow_checks = 0
                while runtime.time_seconds < args.seconds:
                    before = runtime.time_seconds
                    previous_count = len(controller.history)
                    stamp = time.perf_counter()
                    await runtime.tick_async()
                    json.dumps(runtime.frame())
                    elapsed = time.perf_counter()-stamp
                    rows = controller.history[previous_count:]
                    round_seconds += controller.last_round_seconds
                    step_timings.append({'from': before, 'to': runtime.time_seconds, 'wallSeconds': elapsed,
                                         'decisions': len(rows), 'modelRoundSeconds': controller.last_round_seconds})
                    for row in rows:
                        audit_context(row['context'])
                        assert row['person_id'] not in runtime.class2_flows.background_ids
                        assert row['execution']['status'] == 'applied'
                        trace.write(json.dumps(row, ensure_ascii=False) + '\n')
                    if rows:
                        trace.flush()
                        print(json.dumps({'time': runtime.time_seconds, 'decisions': len(rows),
                                          'actions': dict(Counter(row['decision']['action'] for row in rows)),
                                          'fallbacks': sum(row['fallback_error'] is not None for row in rows),
                                          'stepWallSeconds': round(elapsed, 3),
                                          'wallSeconds': round(time.perf_counter()-started, 2)}), flush=True)
                    steps += 1
                    assert set(runtime.current.persons) == ids
                    assert runtime.population.profiles == profiles
                    for pid, target_position in controller.final_targets.items():
                        assert runtime.class2_flows.people[pid]['destination'] == target_position
                        if runtime.plan_executor.wait_until.get(pid, -1) > runtime.time_seconds:
                            assert runtime.class2_flows.confirmed_speeds[pid] == 0
                            wait_checks += 1
                        if controller.slow_until.get(pid, -1) > runtime.time_seconds:
                            assert runtime.class2_flows.confirmed_speeds[pid] <= runtime.class2_flows.speeds[pid] * .6 + 1e-8
                            slow_checks += 1
                        if controller.eligible(pid):
                            stage = runtime.adapter.current_person_stage(pid)
                            assert stage.edges[-1] == target_position['edge']
                            assert abs(stage.arrivalPos-target_position['position']) < .0001
                distances = {pid: math.hypot(runtime.current.persons[pid].x-t['x'], runtime.current.persons[pid].y-t['y'])
                             for pid, t in controller.final_targets.items()}
                result = {
                    'entrypoint': 'SimulationRuntime.tick_async (same as WebSocket live run)',
                    'runtimeState': runtime.state.value, 'runtimeError': runtime.last_error,
                    'model': model.model, 'simulationSeconds': runtime.time_seconds,
                    'activityCount': len(controller.ids), 'backgroundCount': len(runtime.class2_flows.background_ids),
                    'periodSeconds': controller.period_seconds, 'concurrency': controller.concurrency,
                    'timeoutSeconds': model.timeout_seconds,
                    'modelCalls': model.calls, 'modelSuccesses': model.successes, 'modelFallbacks': model.fallbacks,
                    'fallbackErrors': dict(Counter(row['fallback_error'] for row in controller.history if row['fallback_error'])),
                    'backgroundCycles': len(cycles), 'backgroundHoldSeconds': 5,
                    'actions': dict(Counter(row['decision']['action'] for row in controller.history if not row['fallback_error'])),
                    'executions': dict(Counter(row['execution']['status'] for row in controller.history)),
                    'withLocalAlternatives': sum(bool(row['context']['local_options']) for row in controller.history),
                    'arrivedActivityCount': len(set(controller.ids) & runtime.class2_flows.dwelling),
                    'initialMeanDistanceM': sum(initial_distances.values())/len(distances),
                    'finalMeanDistanceM': sum(distances.values())/len(distances),
                    'movedActivityCount': sum(math.dist(initial_positions[pid], (runtime.current.persons[pid].x, runtime.current.persons[pid].y)) > 1 for pid in controller.ids),
                    'identitiesPreserved': True, 'destinationsPreserved': True, 'localInputAuditPassed': True,
                    'conservationError': runtime.population.diagnostics()['conservation_error'],
                    'wallSeconds': time.perf_counter()-started, 'decisionRoundSeconds': round_seconds,
                    'steps': steps, 'trace': str(trace_path),
                    'stepTimings': step_timings,
                    'stallsOverOneSecond': [row for row in step_timings if row['wallSeconds'] > 1],
                    'stepMaxSeconds': max(row['wallSeconds'] for row in step_timings),
                    'ordinaryStepMeanSeconds': sum(row['wallSeconds'] for row in step_timings if not row['decisions']) / max(1, sum(not row['decisions'] for row in step_timings)),
                    'waitSpeedChecks': wait_checks, 'slowSpeedChecks': slow_checks,
                    'limitations': ['distance-radius perception without obstacle occlusion',
                                    'synchronous decision rounds pause simulation while model responds',
                                    'functional validation, not validation against real pedestrian behaviour'],
                }
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if not model.successes:
                    raise RuntimeError('no successful real model decisions; see trace for fallback errors')
            finally:
                runtime.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requirement', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=100)
    parser.add_argument('--timeout', type=float, default=None)
    parser.add_argument('--concurrency', type=int, default=None)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
