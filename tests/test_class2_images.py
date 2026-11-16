"""Temporary archive checks only; no servers, models or SUMO."""
from copy import deepcopy
import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from crowdsim.experiments.class2_images import Class2ComparisonImages
from crowdsim.experiments.contracts import ContractError
from crowdsim.experiments.serve import ControlRequestHandler, CONTROL_PREFIX
from tests.test_class2_comparison import setup, CID
from tests.test_during_observation_images import case, no_live_services


@pytest.fixture
def images(setup, case):
    comparisons, gateway, _, body, _ = setup
    record = comparisons.configure('user', 'during-' + CID, CID, body)
    record = comparisons._read(CID)
    row = record['runs'][0]
    row.update(status='completed', attempt=1, runtimeRunId=case.archive.name,
               requirementId='req-one', artificialSociety={'model_id': 'asm-one'})
    comparisons._write(record)
    manifest = {'run_id': row['runtimeRunId'], 'requirement': {'requirement_id': row['requirementId']},
                'scenario': {'network_sha256': record['networkSha256'],
                    'class2_artificial_society': row['artificialSociety'],
                    'class2_demo': {'source': {'conversationId': CID, 'snapshotId': record['snapshotId']},
                                   'seed': row['seed'], 'flow_design': record['executionRequest']['flowDesign']}}}
    (case.archive / 'manifest.json').write_text(json.dumps(manifest))
    geometry = json.loads((case.archive / 'observation_geometry.json').read_text())
    geometry['network_sha256'] = record['networkSha256']
    (case.archive / 'observation_geometry.json').write_text(json.dumps(geometry))
    service = Class2ComparisonImages(comparisons, runs_root=case.archive.parent, renderer=case.service.renderer)
    expected = {'snapshotId': record['snapshotId'], 'executionVersion': record['executionVersion'], 'attempt': 1}
    return SimpleNamespace(service=service, comparisons=comparisons, row=row, record=record, expected=expected,
                           archive=case.archive, calls=case.calls, gateway=gateway)


def get(c):
    return c.service.metadata('user', 'during-' + CID, CID, c.row['id'], c.expected)


def test_lazy_six_images_use_shared_renderer_cache_and_leave_experiments_untouched(images):
    c = images
    before = deepcopy(c.comparisons._read(CID))
    assert not c.calls and not c.service.cache_root.exists()
    result = get(c)
    assert [m['timeSeconds'] for m in result['moments']] == [.5, 5, 10]
    assert all(len(m['images']) == 2 for m in result['moments'])
    assert result['candidateId'] == c.row['candidateId'] and result['attempt'] == 1
    assert get(c) == result and len(c.calls) == 1
    assert c.comparisons._read(CID) == before and not c.gateway.commands
    content = c.service.image('user', 'during-' + CID, CID, c.row['id'], c.expected, result['sourceKey'], 'scene-start.png')
    assert content.startswith(b'\x89PNG')


def test_foreign_scopes_attempts_and_archives_are_rejected(images):
    c = images
    with pytest.raises(ValueError): c.service.metadata('other', 'during-' + CID, CID, c.row['id'], c.expected)
    with pytest.raises(ValueError): c.service.metadata('user', 'during-other', CID, c.row['id'], c.expected)
    for field, value in [('attempt', 2), ('snapshotId', 'other'), ('executionVersion', 8)]:
        with pytest.raises(ContractError):
            c.service.metadata('user', 'during-' + CID, CID, c.row['id'], {**c.expected, field: value})
    path = c.archive / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['scenario']['class2_demo']['source']['conversationId'] = 'other'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ContractError): get(c)
    assert not c.calls


def test_running_missing_and_incomplete_frames_do_not_invent_pictures(images):
    c = images
    c.record['runs'][0]['status'] = 'running'; c.comparisons._write(c.record)
    assert get(c)['moments'] == [] and not c.calls
    c.record['runs'][0]['status'] = 'completed'; c.comparisons._write(c.record)
    path = c.archive / 'observation_global.csv'
    path.write_text(path.read_text().replace(',1', ',2'))
    assert all(m['images'] == {} for m in get(c)['moments'])
    (c.archive / 'trajectory.csv').unlink()
    assert get(c)['message'] == '本轮原始图像数据未归档'


def test_rerun_during_render_cannot_publish_or_read_old_images(images):
    c = images
    result = get(c)
    c.record['runs'][0]['attempt'] = 2; c.comparisons._write(c.record)
    with pytest.raises(ContractError):
        c.service.image('user', 'during-' + CID, CID, c.row['id'], c.expected, result['sourceKey'], 'scene-start.png')
    c.expected['attempt'] = 2
    def rerun(*args, **kwargs):
        c.record['runs'][0]['attempt'] = 3; c.comparisons._write(c.record)
    c.service.renderer = rerun
    with pytest.raises(ContractError): get(c)
    assert len(list(c.service.cache_root.glob('*/index.json'))) == 1


def test_http_dispatch_uses_current_attempt_for_json_and_png(images, monkeypatch):
    c = images
    monkeypatch.setattr('crowdsim.experiments.class2_images.Class2ComparisonImages', lambda _: c.service)
    handler = object.__new__(ControlRequestHandler)
    handler.server = SimpleNamespace(bundle=SimpleNamespace(class2_comparisons=c.comparisons))
    handler.headers = {'X-CrowdSim-User': 'user', 'X-CrowdSim-Workspace': 'during-' + CID}
    path = f'{CONTROL_PREFIX}/during-class2/conversations/{CID}/comparisons/runs/{c.row["id"]}/images'
    query = f'?snapshotId={c.expected["snapshotId"]}&executionVersion={c.expected["executionVersion"]}&attempt=1'
    results = []
    handler._send_json = lambda status, data, **kwargs: results.append((status, data))
    handler.path = path + query; handler.do_GET()
    assert results[0][0] == 200
    headers = []
    handler.send_response = lambda status: headers.append(('status', status))
    handler.send_header = lambda k, v: headers.append((k, v))
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()
    handler.path = path + '/' + results[0][1]['sourceKey'] + '/scene-start.png' + query
    handler.do_GET()
    assert ('Content-Type', 'image/png') in headers and handler.wfile.getvalue().startswith(b'\x89PNG')
