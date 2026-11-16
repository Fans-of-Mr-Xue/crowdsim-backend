"""On-demand pictures of completed class-two attempts, using archived positions only."""
from copy import deepcopy
import json
from pathlib import Path
import re

from dialog_storage.during_workflow import confirmed_workflow
from .contracts import ContractError
from .during_observation_images import ROOT, FILES, digest, render_archive_images


class Class2ComparisonImages:
    def __init__(self, comparisons, *, runs_root=None, renderer=None):
        self.comparisons = comparisons
        self.runs_root = Path(runs_root or ROOT / 'runs').resolve()
        self.cache_root = comparisons.root / 'images'
        self.renderer = renderer

    def _context(self, user, workspace, cid, rid, expected):
        with self.comparisons._condition:
            record = self.comparisons._scope(user, workspace, cid)
            row = next((r for r in (record or {}).get('runs', []) if r['id'] == rid), None)
            if not row:
                raise LookupError('RUN_NOT_FOUND')
            requirement = self.comparisons.requirements.load(cid)['state']['requirement']
            workflow = confirmed_workflow(cid, requirement)
            if (workflow['problemType'] != 2 or workflow['snapshotId'] != record['snapshotId']
                    or expected.get('snapshotId') != record['snapshotId']
                    or any(type(expected.get(k)) is not int or expected[k] != value for k, value in
                           [('executionVersion', record['executionVersion']), ('attempt', row['attempt'])])):
                raise ContractError('实验或重跑版本已变化，请重新读取评估。')
            identity = {k: record[k] for k in ('conversationId', 'snapshotId', 'experimentId', 'executionVersion')}
            identity.update(runId=rid, candidateId=row['candidateId'], attempt=row['attempt'],
                            runtimeRunId=row.get('runtimeRunId'))
            return deepcopy(record), deepcopy(row), identity

    def _archive(self, record, row, identity):
        runtime = identity['runtimeRunId']
        if not isinstance(runtime, str) or not re.fullmatch(r'run-[0-9a-f]{32}', runtime):
            raise ContractError('本轮没有可读取的原始运行归档。')
        directory = (self.runs_root / runtime).resolve()
        if directory.parent != self.runs_root:
            raise ContractError('运行归档路径无效。')
        manifest = json.loads((directory / 'manifest.json').read_text())
        geometry = json.loads((directory / 'observation_geometry.json').read_text())
        scenario = manifest.get('scenario') or {}
        demo = scenario.get('class2_demo') or {}
        if (manifest.get('run_id') != runtime
                or demo.get('source') != {k: identity[k] for k in ('conversationId', 'snapshotId')}
                or demo.get('seed') != row['seed']
                or demo.get('flow_design') != record['executionRequest']['flowDesign']
                or (manifest.get('requirement') or {}).get('requirement_id') != row.get('requirementId')
                or not row.get('artificialSociety') or scenario.get('class2_artificial_society') != row['artificialSociety']
                or geometry.get('network_sha256') != record['networkSha256']
                or scenario.get('network_sha256') != record['networkSha256']):
            raise ContractError('原始归档与本轮会话、模型或路网不一致。')
        names = ['manifest.json', 'observation_geometry.json', 'observation_samples.jsonl',
                 'observation_global.csv', 'trajectory.csv']
        stamps = {n: [p.stat().st_size, p.stat().st_mtime_ns] for n in names for p in [directory / n]}
        key = digest({'renderer': 1, 'identity': identity, 'row': row, 'files': stamps})
        return directory, geometry, manifest, key

    def _assert_current(self, user, workspace, cid, rid, expected, key):
        record, row, identity = self._context(user, workspace, cid, rid, expected)
        if row['status'] != 'completed' or self._archive(record, row, identity)[3] != key:
            raise ContractError('实验图像来源已变化，请重新读取。')

    def metadata(self, user, workspace, cid, rid, expected):
        record, row, identity = self._context(user, workspace, cid, rid, expected)
        if row['status'] != 'completed':
            return {**identity, 'moments': [], 'message': '本轮完成后可生成截图'}
        try:
            directory, geometry, manifest, key = self._archive(record, row, identity)
        except FileNotFoundError:
            return {**identity, 'moments': [], 'message': '本轮原始图像数据未归档'}
        candidate = next(c for c in record['candidates'] if c['id'] == row['candidateId'])
        return render_archive_images(directory, geometry, manifest, identity, key,
            cache_root=self.cache_root, status=row['status'], duration=record['executionRequest']['durationSeconds'],
            title=f"{record['executionRequest']['projectTitle']} · {candidate['label']}", round_number=row['round'],
            renderer=self.renderer, assert_current=lambda: self._assert_current(user, workspace, cid, rid, expected, key))

    def image(self, user, workspace, cid, rid, expected, key, filename):
        if not re.fullmatch(r'[0-9a-f]{64}', key) or filename not in FILES:
            raise ContractError('图像标识无效。')
        self._assert_current(user, workspace, cid, rid, expected, key)
        directory = self.cache_root / key
        metadata = json.loads((directory / 'index.json').read_text())
        if not any(filename in m.get('images', {}).values() for m in metadata['moments']):
            raise ContractError('该时刻没有归档图像。')
        return (directory / filename).read_bytes()
