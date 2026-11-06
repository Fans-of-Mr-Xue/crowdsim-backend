"""Check real SUMO materialization against synthetic frozen map previews.

No HTTP, browser, database, remote model, or production service is used.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crowdsim.scenarios.class2_crowding import load_preset, prepare_requirement
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim_overlay_server import parse_args, build_requirement_runtime
import crowdsim.core.simulation_runtime as runtime_module


def check_case(case, manifest, folder):
    preset = load_preset()[0]
    base = json.loads((ROOT/preset['requirement_template']).read_text())['requirement']
    count = sum(flow['count'] for flow in case['design']['flows'])
    body = {'problemType': 2, 'count': count, 'phenomenon': '合成人流配置与真实SUMO生成结果核对',
        'requestedLocation': '人民英雄纪念塔', 'durationSeconds': case.get('duration', 30),
        'source': {'conversationId': 'synthetic-flow-check', 'snapshotId': case['name']},
        'projectTitle': case['name'], 'flowDesign': case['design'], 'mapFingerprint': manifest['mapFingerprint'],
        'populationDistributions': base['population']['distributions'],
        'metricIds': ['global-density', 'local-density', 'global-speed', 'local-speed', 'boundary-density-difference']}
    spec = prepare_requirement(body)
    record = RequirementRepository(folder/'requirements').create(spec, purpose='experiment')
    runtime = build_requirement_runtime(parse_args(['--scenario', 'hotspot']), record)
    try:
        with patch.object(runtime_module, 'PROJECT_ROOT', folder):
            runtime.initialize()
        controller, conn = runtime.class2_flows, runtime.adapter.connection
        layout, people = controller.layout, runtime.current.persons
        assert len(people) == count
        assert set(people) == set(controller.people)
        assert runtime.init_frame()['metrics']['class2_flows']['total'] == count
        assert all(p.speed == 0 for p in people.values())
        preview_error, materialization_error, destination_error = [], [], []
        by_flow = Counter()
        for f, expected, built in zip(case['design']['flows'], case['expected'], layout['flows']):
            assert f['id'] == built['id'] == expected['id']
            assert abs(built['origin_layout']['area']-f['count']/f['density']) < 1e-5
            assert abs(built['destination_layout']['area']-f['count']/f['density']) < 1e-5
            points = [p for p in layout['positions'] if p['flow_id'] == f['id']]
            for planned, point, target in zip(points, expected['origin']['points'], expected['destination']['points']):
                pid = planned['person_id']; actual = people[pid]; by_flow[f['id']] += 1
                preview_error.append(math.dist([planned['local_x'], planned['local_y']], point))
                destination_error.append(math.dist([planned['destination']['local_x'], planned['destination']['local_y']], target))
                materialization_error.append(math.dist([actual.x, actual.y], [point[0]+manifest['originXY'][0], point[1]+manifest['originXY'][1]]))
                assert actual.edge_id == planned['edge']
                assert abs(actual.lane_position-planned['position']) < .001
                assert abs(conn.person.getMaxSpeed(pid)-f['speed']) < 1e-7 or pid in controller.dwelling
                assert abs(runtime.plan_executor.base_limits[pid]-f['speed']) < 1e-7
                stage = conn.person.getStage(pid)
                if pid not in controller.dwelling:
                    assert stage.edges[-1] == planned['destination']['edge']
                    assert abs(stage.arrivalPos-planned['destination']['position']) < .001
        assert dict(by_flow) == {f['id']: f['count'] for f in case['design']['flows']}
        assert max(preview_error, default=0) < .001, ('preview mismatch', max(preview_error))
        assert max(destination_error, default=0) < .001
        assert max(materialization_error, default=0) < .002, ('SUMO position mismatch', max(materialization_error))
        speed_peaks = dict.fromkeys(by_flow, 0.0)
        runtime.start()
        while runtime.state.value == 'RUNNING':
            if not case.get('finish') and runtime.snapshot_index >= case.get('steps', 3):
                break
            runtime.tick()
            assert len(runtime.current.persons) == count
            for pid, motion in runtime.current.persons.items():
                f = controller.people[pid]
                speed_peaks[f['flow_id']] = max(speed_peaks[f['flow_id']], motion.speed)
                assert motion.speed <= f['speed']+1e-6
        if case.get('finish'):
            assert runtime.time_seconds == body['durationSeconds']
            assert len(controller.dwelling) == count
            assert runtime.population.diagnostics()['arrived'] == 0
            assert all(p.speed == 0 and p.stage_type == 2 for p in runtime.current.persons.values()), [
                {'id': p.person_id, 'speed': p.speed, 'stage': p.stage_type, 'position': p.lane_position}
                for p in runtime.current.persons.values() if p.speed != 0 or p.stage_type != 2]
            assert all(abs(p.lane_position-controller.people[pid]['destination']['position']) <= .061
                       for pid, p in runtime.current.persons.items())
        diagnostics = runtime.population.diagnostics()
        assert diagnostics['conservation_error'] == 0
        return {'name': case['name'], 'passed': True, 'sumoVersion': runtime.adapter.diagnostics['sumo_version'],
            'count': count, 'flowCounts': dict(by_flow), 'previewMaxErrorMeters': max(preview_error, default=0),
            'sumoMaxPositionErrorMeters': max(materialization_error, default=0),
            'destinationMaxPreviewErrorMeters': max(destination_error, default=0), 'speedPeaksMps': speed_peaks,
            'configuredSpeedsMps': {f['id']: f['speed'] for f in case['design']['flows']},
            'dwelling': len(controller.dwelling), 'timeSeconds': runtime.time_seconds,
            'population': diagnostics, 'configuration': case['design'], 'runDirectory': str(folder/'runs'/runtime.run_id)}
    finally:
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    manifest = json.loads(options.cases.read_text())
    folder = Path(tempfile.mkdtemp(prefix='crowdsim-flow-sumo-'))
    report = {'syntheticOnly': True, 'browserUsed': False, 'servicesRestarted': False, 'cases': []}
    for case in manifest['cases']:
        try:
            result = check_case(case, manifest, folder/case['name'])
        except Exception as error:
            result = {'name': case['name'], 'passed': False, 'error': f'{type(error).__name__}: {error}', 'traceback': traceback.format_exc()}
        report['cases'].append(result)
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print(('PASS ' if result['passed'] else 'FAIL ') + case['name'] + (' '+result['error'] if not result['passed'] else ''), flush=True)
    report['passed'] = all(case['passed'] for case in report['cases'])
    options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
