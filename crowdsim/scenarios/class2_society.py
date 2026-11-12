"""Published artificial-society recipe for the existing class-two flow engine.

This binds the actual flow realization and frozen modeling inputs without
enabling the different class-one population/controller pipeline.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile

from dialog_storage.during_requirements import DuringRequirementRepository
from dialog_storage.during_workflow import confirmed_workflow

ROOT = Path(__file__).resolve().parents[2]


def _root():
    return Path(os.environ.get('CROWDSIM_RUNS_ROOT') or ROOT / 'runs')


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def publish_class2_society(receipt, *, requirements=None, root=None):
    binding = receipt['class2_demo']
    source = binding['source']
    requirements = requirements or DuringRequirementRepository(ROOT / 'runs/dialogues/during-requirements')
    saved = requirements.load(source['conversationId'])
    if not saved:
        raise ValueError('第二类人工社会构建需要本会话的后端确认快照')
    record = saved['state']['requirement']
    workflow = confirmed_workflow(source['conversationId'], record)
    snapshot = record['snapshots'][0]
    r = snapshot['requirement']
    if workflow['snapshotId'] != source['snapshotId'] or workflow['problemType'] != 2:
        raise ValueError('人工社会来源与当前会话或问题类型不一致')
    design = deepcopy(r.get('class2', {}).get('flow_design'))
    if design:
        design['flows'] = [{key: flow.get(key) for key in ('id', 'name', 'count', 'density', 'speed', 'origin', 'destination')} for flow in design.get('flows', [])]
    if (r.get('population', {}).get('total') != binding['count'] or design != binding['flow_design']
            or r.get('population', {}).get('distributions') != receipt['requirement']['population']['distributions']
            or r.get('experiment', {}).get('duration_seconds') != binding['duration_seconds']
            or r.get('observation', {}).get('metric_ids') != receipt['requirement']['observation']['metric_ids']):
        raise ValueError('第二类执行配置与确认的人工社会需求不一致')
    payload = {'schemaVersion': 'class2-artificial-society-1', 'source': source,
               'requirementId': receipt['requirement_id'], 'requirementFingerprint': receipt['fingerprint'],
               'modelingRequirements': snapshot['modeling_requirements'],
               'population': r['population'], 'flowDesign': design,
               'networkSha256': design['network_sha256'], 'layoutSha256': binding['layout_sha256'],
               'engine': 'class2-flow-runtime-v1'}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    model = {'model_id': 'asm-' + digest[:32], 'fingerprint': 'sha256:' + digest,
             'status': 'published', 'requirement_id': receipt['requirement_id'], 'source': source,
             'artificial_society': payload, 'experimentId': workflow['experimentId'],
             'capabilities': {'flow_layout': 'runtime_consumed', 'flow_speed': 'runtime_consumed',
                              'modeling_requirements': 'frozen_recipe', 'engine': 'class2-flow-runtime-v1'}}
    directory = Path(root or _root()) / 'artificial_societies/class2'
    _write(directory / (model['model_id'] + '.json'), model)
    _write(directory / (receipt['requirement_id'] + '.json'), {'model_id': model['model_id'], 'source': source})
    return model


def load_class2_society(requirement_id, source, *, root=None):
    directory = Path(root or _root()) / 'artificial_societies/class2'
    index = json.loads((directory / f'{requirement_id}.json').read_text())
    model = json.loads((directory / (index['model_id'] + '.json')).read_text())
    digest = hashlib.sha256(json.dumps(model['artificial_society'], sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if model.get('fingerprint') != 'sha256:' + digest or model.get('model_id') != 'asm-' + digest[:32]:
        raise ValueError('第二类人工社会模型内容指纹不匹配')
    if model['requirement_id'] != requirement_id or model['source'] != source or index['source'] != source:
        raise ValueError('人工社会模型与本次推演来源不一致')
    return model


def society_binding(model):
    return {key: model[key] for key in ('model_id', 'fingerprint', 'status', 'requirement_id', 'source', 'experimentId')}
