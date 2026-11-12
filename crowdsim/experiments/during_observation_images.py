"""Lazy, attempt-bound class-one pictures from committed archives. No engine calls."""
from copy import deepcopy
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import threading

from .contracts import ContractError

ROOT = Path(__file__).resolve().parents[2]
BINDING = ('conversationId', 'snapshotId', 'requirementId', 'requirementFingerprint', 'modelId', 'modelFingerprint')
MOMENTS = ('start', 'middle', 'end')
FILES = {f'{kind}-{moment}.png' for kind in ('scene', 'heatmap') for moment in MOMENTS}
RENDER_LOCK = threading.Lock()  # Matplotlib and its font manager are process-global.


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class DuringObservationImages:
    def __init__(self, repository, *, runs_root=None, cache_root=None, renderer=None):
        self.repository = repository
        self.runs_root = Path(runs_root or ROOT / 'runs').resolve()
        self.cache_root = Path(cache_root or (repository._root or self.runs_root) / 'observation_images')
        self.renderer = renderer

    def _context(self, user, workspace, eid, rid, expected):
        with self.repository._lock:
            experiment = self.repository.get_experiment(user, workspace, eid)
            run = self.repository.get_run(user, workspace, rid)
            if not experiment or not run or run['experiment_id'] != eid:
                raise LookupError('RUN_NOT_FOUND')
            binding = experiment.get('duringObservation') or {}
            if (not all(binding.get(k) for k in BINDING) or workspace != 'during-' + binding['conversationId']
                    or binding.get('requirement', {}).get('research', {}).get('problem_type') != 1):
                raise ContractError('图像仅用于当前会话的第一类实验。')
            identity = {**{k: binding[k] for k in BINDING}, 'experimentId': eid, 'runId': rid,
                        'executionVersion': experiment.get('executionVersion', 0),
                        'restartCount': run.get('restartCount', 0), 'runtimeRunId': run.get('runtime_run_id')}
            if (expected.get('snapshotId') != identity['snapshotId']
                    or any(type(expected.get(k)) is not int or expected[k] != identity[k]
                           for k in ('executionVersion', 'restartCount')) or run.get('restartPending')):
                raise ContractError('实验或重跑版本已变化，请重新展开本轮图像。')
            return deepcopy(experiment), deepcopy(run), identity

    def _archive(self, experiment, run, identity):
        runtime = identity['runtimeRunId']
        if not isinstance(runtime, str) or not re.fullmatch(r'run-[0-9a-f]{32}', runtime):
            raise ContractError('本轮没有可读取的原始运行归档。')
        directory = self.runs_root / runtime
        if directory.resolve().parent != self.runs_root:
            raise ContractError('运行归档路径无效。')
        manifest = json.loads((directory / 'manifest.json').read_text())
        geometry = json.loads((directory / 'observation_geometry.json').read_text())
        society = json.loads((directory / 'artificial_society.json').read_text())
        receipt = manifest.get('requirement') or {}
        evidence = run.get('observationBinding') or {}
        if (manifest.get('run_id') != runtime or receipt.get('requirement_id') != identity['requirementId']
                or receipt.get('fingerprint') != identity['requirementFingerprint']
                or society.get('model_id') != identity['modelId'] or society.get('fingerprint') != identity['modelFingerprint']
                or any(evidence.get(k) != identity[k] for k in BINDING)
                or digest(geometry) != evidence.get('geometryFingerprint')
                or geometry.get('network_sha256') != (manifest.get('scenario') or {}).get('network_sha256')):
            raise ContractError('原始归档与本轮会话、模型或观测区域不一致。')
        paths = ['manifest.json', 'artificial_society.json', 'observation_geometry.json',
                 'observation_samples.jsonl', 'observation_global.csv', 'trajectory.csv']
        stamps = {p: [f.stat().st_size, f.stat().st_mtime_ns] for p in paths for f in [directory / p]}
        key = digest({'renderer': 1, 'identity': identity, 'status': run['status'], 'files': stamps})
        return directory, geometry, manifest, key

    def metadata(self, user, workspace, eid, rid, expected):
        experiment, run, identity = self._context(user, workspace, eid, rid, expected)
        if run['status'] not in {'completed', 'failed', 'cancelled'}:
            return {**identity, 'moments': [], 'message': '本轮结束后显示图像'}
        if not identity['runtimeRunId']:
            return {**identity, 'moments': [], 'message': '本轮没有运行图像'}
        try:
            directory, geometry, manifest, key = self._archive(experiment, run, identity)
        except FileNotFoundError:
            return {**identity, 'moments': [], 'message': '本轮原始图像数据未归档'}
        target = self.cache_root / key
        with RENDER_LOCK:
            # Check the attempt again after waiting for another render.
            self._assert_current(user, workspace, eid, rid, expected, key)
            index = target / 'index.json'
            if index.is_file():
                saved = json.loads(index.read_text())
                if saved.get('sourceKey') == key and all(saved.get(k) == v for k, v in identity.items()) and all((target / name).is_file()
                        for m in saved['moments'] for name in m.get('images', {}).values()):
                    return saved
            samples = [json.loads(line) for line in (directory / 'observation_samples.jsonl').read_text().splitlines() if line.strip()]
            times = [s.get('time_seconds') for s in samples]
            if (not times or any(type(t) not in (float, int) or not math.isfinite(t) for t in times)
                    or any(b <= a for a, b in zip(times, times[1:]))):
                return {**identity, 'moments': [], 'message': '本轮没有完整的时刻记录'}
            completed = run['status'] == 'completed'
            midpoint = (times[0] + times[-1]) / 2 if completed else float(experiment['scenario']['durationSeconds']) / 2
            chosen = [samples[0], min(samples, key=lambda s: (abs(s['time_seconds'] - midpoint), s['time_seconds']))
                      if completed or times[-1] >= midpoint else None, samples[-1] if completed else None]
            frames = {s['snapshot_id']: {} for s in chosen if s}
            with (directory / 'trajectory.csv').open() as handle:
                for row in csv.DictReader(handle):
                    if row.get('snapshot_id') in frames:
                        frames[row['snapshot_id']][row['person_id']] = row
            globals_ = {}
            with (directory / 'observation_global.csv').open() as handle:
                for row in csv.DictReader(handle):
                    if row.get('snapshot_id') in frames:
                        globals_[row['snapshot_id']] = row
            moments = []
            for name, sample in zip(MOMENTS, chosen):
                moment = {'key': name, 'timeSeconds': sample['time_seconds'] if sample else None, 'images': {}}
                if sample:
                    sid, time = sample['snapshot_id'], sample['time_seconds']
                    people = list(frames[sid].values())
                    count = globals_.get(sid, {}).get('network_person_count')
                    valid = count is not None and float(count) == len(people)
                    valid = valid and all(float(p['time']) == time and all(math.isfinite(float(p[k])) for k in ('x', 'y')) for p in people)
                    if valid:
                        moment.update(personCount=len(people), snapshotId=sid, images={kind: f'{kind}-{name}.png' for kind in ('scene', 'heatmap')})
                    else:
                        moment['message'] = '该时刻的位置记录不完整'
                else:
                    moment['message'] = '未记录该时刻'
                moments.append(moment)
            result = {**identity, 'sourceKey': key, 'moments': moments}
            self.cache_root.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix='render-', dir=self.cache_root))
            try:
                if self.renderer:
                    renderer = self.renderer
                else:
                    from .during_observation_image_render import render_images
                    renderer = render_images
                if any(m['images'] for m in moments):
                    renderer(temporary, geometry, manifest, moments, frames,
                             title=experiment['duringObservation']['requirement'].get('project', {}).get('title', '实验'),
                             round_number=next(i + 1 for i, r in enumerate(self.repository.list_runs(user, workspace, eid)) if r['run_id'] == rid))
                self._assert_current(user, workspace, eid, rid, expected, key)
                (temporary / 'index.json').write_text(json.dumps(result, ensure_ascii=False))
                # Only a complete manifest makes an image set visible.
                target.mkdir(parents=True, exist_ok=True)
                for p in temporary.iterdir():
                    if p.name != 'index.json':
                        p.replace(target / p.name)
                (temporary / 'index.json').replace(index)
                return result
            finally:
                shutil.rmtree(temporary)

    def _assert_current(self, user, workspace, eid, rid, expected, key):
        experiment, run, identity = self._context(user, workspace, eid, rid, expected)
        if self._archive(experiment, run, identity)[3] != key:
            raise ContractError('实验图像来源已变化，请重新读取。')

    def image(self, user, workspace, eid, rid, expected, key, filename):
        if not re.fullmatch(r'[0-9a-f]{64}', key) or filename not in FILES:
            raise ContractError('图像标识无效。')
        self._assert_current(user, workspace, eid, rid, expected, key)
        directory = self.cache_root / key
        metadata = json.loads((directory / 'index.json').read_text())
        if not any(filename in m.get('images', {}).values() for m in metadata['moments']):
            raise ContractError('该时刻没有归档图像。')
        return (directory / filename).read_bytes()
