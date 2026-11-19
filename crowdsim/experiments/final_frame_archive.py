"""Immutable, scoped real final frames. No simulator commands or frame reconstruction."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import math
import os
import tempfile

BINDING_KEYS = ('conversationId', 'snapshotId', 'requirementId', 'requirementFingerprint', 'modelId', 'modelFingerprint')


def identity(experiment, run):
    binding = experiment.get('duringObservation') or {}
    if not all(isinstance(binding.get(k), str) and binding[k] for k in BINDING_KEYS):
        raise ValueError('FINAL_FRAME_BINDING_INVALID')
    if not run.get('runtime_run_id') or run.get('experiment_id') != experiment.get('experiment_id'):
        raise ValueError('FINAL_FRAME_RUN_INVALID')
    return {'experimentId': experiment['experiment_id'], 'runId': run['run_id'],
            'runtimeRunId': run['runtime_run_id'], 'restartCount': run.get('restartCount', 0),
            'binding': {k: binding[k] for k in BINDING_KEYS},
            'scope': [str(run['user_id']), str(run['workspace_id'])]}


def validate_view(key, view):
    init, frame = view.get('init'), view.get('frame')
    if not isinstance(init, dict) or not isinstance(frame, dict):
        raise ValueError('FINAL_FRAME_MISSING')
    runtime = key['runtimeRunId']; binding = key['binding']
    if (view.get('runId') != key['runId'] or init.get('run_id') != runtime
            or frame.get('run_id') != runtime or frame.get('runtime_state') != 'FINISHED'
            or frame.get('type') != 'update' or not isinstance(frame.get('snapshot_id'), str)
            or not frame['snapshot_id'].startswith(runtime + ':')):
        raise ValueError('FINAL_FRAME_IDENTITY_INVALID')
    req, model = init.get('requirement') or {}, init.get('artificial_society') or {}
    if (req.get('requirement_id') != binding['requirementId'] or req.get('fingerprint') != binding['requirementFingerprint']
            or model.get('model_id') != binding['modelId'] or model.get('fingerprint') != binding['modelFingerprint']):
        raise ValueError('FINAL_FRAME_MODEL_INVALID')
    time = frame.get('step_seconds')
    if type(time) not in (int, float) or not math.isfinite(time) or time < 0:
        raise ValueError('FINAL_FRAME_TIME_INVALID')
    for kind in ('pedestrians', 'vehicles'):
        people = frame.get(kind)
        if not isinstance(people, list):
            raise ValueError('FINAL_FRAME_ENTITIES_INVALID')
        ids = set()
        for person in people:
            if not isinstance(person, dict) or not isinstance(person.get('id'), str) or not person['id'] or person['id'] in ids:
                raise ValueError('FINAL_FRAME_ENTITIES_INVALID')
            ids.add(person['id'])
            if person.get('synthetic'):
                raise ValueError('FINAL_FRAME_SYNTHETIC')
            state = person.get('state') or {}; agent = state.get('agent_runtime')
            if agent is not None and (agent.get('run_id') != runtime or agent.get('snapshot_id') != frame['snapshot_id']
                    or agent.get('person_id') != person['id'] or agent.get('time_seconds') != time
                    or agent.get('model_id') != binding['modelId'] or agent.get('model_fingerprint') != binding['modelFingerprint']):
                raise ValueError('FINAL_FRAME_AGENT_INVALID')
    # Validate the complete payload, not only selected numerical fields.
    json.dumps(view, ensure_ascii=False, allow_nan=False)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


class FinalFrameArchive:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else None
        self.memory = {}

    def _name(self, key):
        return hashlib.sha256(canonical(key)).hexdigest()

    def save(self, experiment, run, view):
        key = identity(experiment, run)
        payload = {k: deepcopy(view[k]) for k in ('runId', 'init', 'frame')}
        validate_view(key, payload)
        record = {'schema': 'crowdsim-final-frame-v1', 'identity': key, 'view': payload}
        record['sha256'] = hashlib.sha256(canonical(record)).hexdigest()
        name = self._name(key)
        if self.root is None:
            existing = self.memory.get(name)
            if existing is not None and existing != record:
                raise ValueError('FINAL_FRAME_CONFLICT')
            self.memory[name] = deepcopy(record)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / (name + '.json')
            if path.exists():
                if json.loads(path.read_text(encoding='utf-8')) != record:
                    raise ValueError('FINAL_FRAME_CONFLICT')
            else:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.root, prefix='.final-', suffix='.tmp', delete=False) as handle:
                        temporary = Path(handle.name); handle.write(canonical(record)); handle.flush(); os.fsync(handle.fileno())
                    temporary.replace(path)
                finally:
                    if temporary is not None: temporary.unlink(missing_ok=True)
        return record['sha256']

    def read(self, experiment, run, *, expected_sha256=None):
        key = identity(experiment, run); name = self._name(key)
        if self.root is None:
            record = deepcopy(self.memory.get(name))
        else:
            path = self.root / (name + '.json')
            if not path.is_file(): return None
            record = json.loads(path.read_text(encoding='utf-8'))
        if record is None: return None
        digest = record.pop('sha256', None)
        if (expected_sha256 is not None and digest != expected_sha256
                or record.get('schema') != 'crowdsim-final-frame-v1' or record.get('identity') != key
                or digest != hashlib.sha256(canonical(record)).hexdigest()):
            raise ValueError('FINAL_FRAME_ARCHIVE_INVALID')
        validate_view(key, record['view'])
        return deepcopy(record['view'])
