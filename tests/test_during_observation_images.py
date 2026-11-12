"""Code-only picture archives: temporary files, no service/model/simulator."""
import csv
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowdsim.experiments.contracts import ContractError
from crowdsim.experiments.during_observation_images import DuringObservationImages, digest
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.serve import CONTROL_PREFIX, ControlRequestHandler


@pytest.fixture(autouse=True)
def no_live_services(monkeypatch):
    import socket
    from crowdsim.infrastructure.sumo_adapter import SumoAdapter
    def forbidden(*args, **kwargs):
        raise AssertionError('Image code checks cannot start a simulator or network service')
    monkeypatch.setattr(SumoAdapter, 'start', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'bind', forbidden)


@pytest.fixture
def case(tmp_path):
    repository = CrowdSimRepository(tmp_path / 'control')
    binding = {'conversationId': 'study', 'snapshotId': 'snapshot', 'requirementId': 'req-one',
               'requirementFingerprint': 'sha256:req', 'modelId': 'asm-one', 'modelFingerprint': 'sha256:model',
               'requirement': {'research': {'problem_type': 1}, 'project': {'title': '测试'}}}
    runtime = 'run-' + 'a'*32
    geometry = {'network_sha256': 'network', 'scope_geometry': {'type': 'Polygon', 'coordinates': [[[0, 0], [20, 0], [20, 30], [0, 0]]]}}
    repository._insert('experiments', {'experiment_id': 'group', 'user_id': 'u', 'workspace_id': 'during-study',
        'duringObservation': binding, 'executionVersion': 2, 'scenario': {'durationSeconds': 10}})
    repository._insert('runs', {'run_id': 'round', 'experiment_id': 'group', 'user_id': 'u', 'workspace_id': 'during-study',
        'runtime_run_id': runtime, 'restartCount': 3, 'status': 'completed',
        'observationBinding': {**binding, 'geometryFingerprint': digest(geometry)}})
    archive = tmp_path / 'runs' / runtime
    archive.mkdir(parents=True)
    for name, data in {'manifest.json': {'run_id': runtime, 'requirement': {'requirement_id': 'req-one', 'fingerprint': 'sha256:req'},
            'scenario': {'network_sha256': 'network'}}, 'artificial_society.json': {'model_id': 'asm-one', 'fingerprint': 'sha256:model'},
            'observation_geometry.json': geometry}.items():
        (archive / name).write_text(json.dumps(data))
    samples = [{'snapshot_id': f'{runtime}:{i}', 'time_seconds': t} for i, t in enumerate([.5, 5, 10])]
    (archive / 'observation_samples.jsonl').write_text(''.join(json.dumps(s)+'\n' for s in samples))
    with (archive / 'trajectory.csv').open('w') as handle:
        writer = csv.DictWriter(handle, ['snapshot_id', 'time', 'person_id', 'x', 'y', 'visual_state'])
        writer.writeheader()
        for sample in samples:
            writer.writerow({'snapshot_id': sample['snapshot_id'], 'time': sample['time_seconds'], 'person_id': 'person', 'x': 1, 'y': 2, 'visual_state': 'normal'})
    with (archive / 'observation_global.csv').open('w') as handle:
        writer = csv.DictWriter(handle, ['snapshot_id', 'network_person_count']); writer.writeheader()
        writer.writerows({'snapshot_id': s['snapshot_id'], 'network_person_count': 1} for s in samples)
    calls = []
    def render(output, geometry, manifest, moments, frames, **kwargs):
        calls.append(moments)
        for m in moments:
            for file in m['images'].values():
                (output / file).write_bytes(b'\x89PNG\r\n\x1a\npreview')
    service = DuringObservationImages(repository, runs_root=tmp_path / 'runs', renderer=render)
    expected = {'snapshotId': 'snapshot', 'executionVersion': 2, 'restartCount': 3}
    return SimpleNamespace(repository=repository, binding=binding, archive=archive, samples=samples,
                           service=service, expected=expected, calls=calls, tmp=tmp_path)


def get(c):
    return c.service.metadata('u', 'during-study', 'group', 'round', c.expected)


def test_three_times_six_images_are_durable_and_cached_without_touching_experiments(case):
    c = case
    before = c.repository.get_run('u', 'during-study', 'round')
    result = get(c)
    assert [m['timeSeconds'] for m in result['moments']] == [.5, 5, 10]
    assert all(len(m['images']) == 2 for m in result['moments'])
    assert result['conversationId'] == 'study' and result['restartCount'] == 3
    assert c.repository.get_run('u', 'during-study', 'round') == before
    assert get(c) == result and len(c.calls) == 1
    restored = DuringObservationImages(c.repository, runs_root=c.tmp / 'runs', renderer=lambda *a, **k: pytest.fail('cached images must survive restart'))
    assert restored.metadata('u', 'during-study', 'group', 'round', c.expected) == result
    assert restored.image('u', 'during-study', 'group', 'round', c.expected, result['sourceKey'], 'scene-start.png').startswith(b'\x89PNG')


@pytest.mark.parametrize('field,value', [('snapshotId', 'other'), ('executionVersion', 1), ('restartCount', 2)])
def test_wrong_snapshot_or_rerun_version_is_rejected_before_render(case, field, value):
    with pytest.raises(ContractError):
        case.service.metadata('u', 'during-study', 'group', 'round', {**case.expected, field: value})
    assert not case.calls


def test_foreign_workspace_second_class_and_foreign_archive_are_rejected(case):
    c = case
    with pytest.raises(LookupError): c.service.metadata('u', 'during-other', 'group', 'round', c.expected)
    c.repository.update_experiment('u', 'during-study', 'group', {'duringObservation': {**c.binding, 'requirement': {'research': {'problem_type': 2}}}})
    with pytest.raises(ContractError): get(c)
    c.repository.update_experiment('u', 'during-study', 'group', {'duringObservation': c.binding})
    path = c.archive / 'artificial_society.json'
    path.write_text(json.dumps({'model_id': 'foreign', 'fingerprint': 'sha256:model'}))
    with pytest.raises(ContractError): get(c)
    assert not c.calls


def test_rerun_and_group_reset_make_old_image_tokens_unreadable(case):
    c = case; old = get(c)
    c.repository.update_run('u', 'during-study', 'round', {'restartCount': 4})
    with pytest.raises(ContractError): c.service.image('u', 'during-study', 'group', 'round', c.expected, old['sourceKey'], 'scene-start.png')
    c.expected['restartCount'] = 4
    new = get(c); assert new['sourceKey'] != old['sourceKey']
    with pytest.raises(ContractError): c.service.image('u', 'during-study', 'group', 'round', c.expected, old['sourceKey'], 'scene-start.png')
    c.repository.update_experiment('u', 'during-study', 'group', {'executionVersion': 3})
    with pytest.raises(ContractError): get(c)


def test_reset_during_render_cannot_publish_old_attempt(case):
    c = case
    c.service.renderer = lambda *args, **kw: c.repository.update_run('u', 'during-study', 'round', {'restartCount': 4})
    with pytest.raises(ContractError): get(c)
    assert not list(c.service.cache_root.glob('*/index.json'))


def test_incomplete_or_cancelled_round_never_invents_an_end_frame(case):
    c = case
    c.repository.update_run('u', 'during-study', 'round', {'status': 'cancelled'})
    result = get(c)
    assert result['moments'][0]['images'] and result['moments'][1]['images']
    assert result['moments'][2]['timeSeconds'] is None and result['moments'][2]['images'] == {}
    path = c.archive / 'observation_global.csv'
    path.write_text(path.read_text().replace(',1', ',2'))
    result = get(c)
    assert all(m['images'] == {} for m in result['moments'])


def test_images_are_not_generated_for_running_or_unstarted_rounds(case):
    for status in ['draft', 'queued', 'running', 'paused']:
        case.repository.update_run('u', 'during-study', 'round', {'status': status})
        assert get(case)['moments'] == []
    assert not case.calls


def test_an_empty_final_scene_is_preserved_when_the_archive_explicitly_records_zero_people(case):
    c = case
    p = c.archive / 'trajectory.csv'
    lines = p.read_text().splitlines(); p.write_text('\n'.join(lines[:-1])+'\n')
    p = c.archive / 'observation_global.csv'
    lines = p.read_text().splitlines(); lines[-1] = lines[-1][:-1]+'0'; p.write_text('\n'.join(lines)+'\n')
    result = get(c)
    assert result['moments'][-1]['personCount'] == 0
    assert len(result['moments'][-1]['images']) == 2


def test_an_early_failure_does_not_label_its_last_frame_as_middle_or_end(case):
    c = case
    c.repository.update_run('u', 'during-study', 'round', {'status': 'failed'})
    (c.archive / 'observation_samples.jsonl').write_text(json.dumps(c.samples[0])+'\n')
    result = get(c)
    assert result['moments'][0]['images']
    assert all(not m['images'] and m['timeSeconds'] is None for m in result['moments'][1:])


def test_http_metadata_and_binary_routes_need_no_listener(case):
    c = case
    handler = object.__new__(ControlRequestHandler)
    handler.server = SimpleNamespace(bundle=SimpleNamespace(observation_images=c.service))
    handler.headers = {'X-CrowdSim-User': 'u', 'X-CrowdSim-Workspace': 'during-study'}
    path = CONTROL_PREFIX + '/experiments/group/runs/round/observation-images'
    query = '?snapshotId=snapshot&executionVersion=2&restartCount=3'
    result = []
    handler._send_json = lambda status, data, **kw: result.append((status, data))
    handler.path = path + query; handler.do_GET()
    assert result[0][0] == 200
    key = result[0][1]['sourceKey']
    headers = []
    handler.send_response = lambda status: headers.append(('status', status))
    handler.send_header = lambda k, v: headers.append((k, v))
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()
    handler.path = path + '/' + key + '/scene-start.png' + query; handler.do_GET()
    assert ('Content-Type', 'image/png') in headers and handler.wfile.getvalue().startswith(b'\x89PNG')


def test_real_renderer_exports_readable_pngs_from_fixture_positions(tmp_path):
    from crowdsim.experiments.during_observation_image_render import render_images
    from PIL import Image
    root = Path(__file__).resolve().parents[1]
    network = root / 'scenarios/shanghai_bund/bund.net.xml'
    geometry = {'network_sha256': hashlib.sha256(network.read_bytes()).hexdigest(),
                'scope_geometry': {'type': 'Polygon', 'coordinates': [[[18490, 5670], [18650, 5670], [18650, 5930], [18490, 5930], [18490, 5670]]]}}
    moments = [{'key': key, 'snapshotId': key, 'timeSeconds': time, 'images': {kind: f'{kind}-{key}.png' for kind in ('scene', 'heatmap')}}
               for key, time in [('start', .5), ('middle', 5), ('end', 10)]]
    frames = {key: {'p': {'x': '18570', 'y': '5830', 'visual_state': 'normal'}} for key in ('start', 'middle', 'end')}
    render_images(tmp_path, geometry, {}, moments, frames, title='代码级图像检查', round_number=1)
    for moment in moments:
        for filename in moment['images'].values():
            with Image.open(tmp_path / filename) as image:
                assert image.size == (1260, 1800)
                image.verify()
