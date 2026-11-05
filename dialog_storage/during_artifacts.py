"""During-only provenance and derived records; simulation evidence stays in runs/."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from .repository import CONVERSATION_ID

RUN_ID = re.compile(r'run-[a-f0-9]{32}')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).encode()).hexdigest()


class ArtifactConflict(ValueError):
    pass


class DuringArtifactRepository:
    def __init__(self, root, requirements, runs_root=None):
        self.root = Path(root)
        self.requirements = requirements
        self.runs_root = Path(runs_root or os.environ.get('CROWDSIM_DURING_ARCHIVE_ROOT')
                              or Path(__file__).resolve().parents[1] / 'runs')
        self.receipts_root = Path(runs_root or os.environ.get('CROWDSIM_RUNS_ROOT')
                                  or Path(__file__).resolve().parents[1] / 'runs') / 'requirements'
        self._lock = threading.RLock()

    def _path(self, cid):
        if not isinstance(cid, str) or not CONVERSATION_ID.fullmatch(cid):
            raise ValueError('无效的事中会话标识')
        return self.root / f'{cid}.json'

    def _snapshots(self, cid):
        envelope = self.requirements.load(cid)
        return {s['id']: s for s in (envelope or {}).get('state', {}).get('requirement', {}).get('snapshots', [])}

    def _run(self, cid, sid, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError('无效的仿真运行标识')
        directory = self.runs_root / run_id
        manifest = json.loads((directory / 'manifest.json').read_text())
        binding = manifest.get('class2_demo') or manifest.get('scenario', {}).get('class2_demo') or {}
        source = binding.get('source') or {}
        if manifest.get('run_id') != run_id or source != {'conversationId': cid, 'snapshotId': sid}:
            raise ValueError('运行存档不属于此会话和方案快照')
        snapshot = self._snapshots(cid).get(sid)
        requirement = (snapshot or {}).get('requirement', {})
        if not snapshot or requirement.get('research', {}).get('problem_type') != 2:
            raise ValueError('运行来源不是此会话已保存的第二类方案')
        if requirement.get('population', {}).get('total') != binding.get('count') or requirement.get('experiment', {}).get('duration_seconds') != binding.get('duration_seconds'):
            raise ValueError('运行人数或时长与已保存方案不一致')
        design = deepcopy(requirement.get('class2', {}).get('flow_design'))
        if design:
            design['flows'] = [{key: flow.get(key) for key in ('id', 'name', 'count', 'density', 'speed', 'origin', 'destination')} for flow in design.get('flows', [])]
            if design != binding.get('flow_design'):
                raise ValueError('运行人流配置与已保存方案不一致')
        requirement_id = (manifest.get('requirement') or {}).get('requirement_id')
        if not isinstance(requirement_id, str) or not re.fullmatch(r'req-[a-f0-9]{32}', requirement_id):
            raise ValueError('仿真存档缺少需求回执')
        receipt = json.loads((self.receipts_root / f'{requirement_id}.json').read_text())
        if receipt.get('requirement_id') != requirement_id or receipt.get('requirement', {}).get('class2_demo', {}).get('source') != source:
            raise ValueError('仿真需求与方案来源不一致')
        receipt['class2_demo'] = receipt['requirement']['class2_demo']
        return {'runId': run_id, 'snapshotId': sid, 'requirementId': requirement_id,
                'receipt': receipt, 'archive': run_id,
                'files': sorted(path.name for path in directory.iterdir() if path.is_file()),
                'createdAt': manifest.get('started_at') or manifest.get('created_at') or datetime.fromtimestamp((directory / 'manifest.json').stat().st_mtime, timezone.utc).isoformat(),
                'networkSha256': manifest.get('scenario', {}).get('network_sha256')}

    def load(self, cid, snapshot_id=None):
        with self._lock:
            path = self._path(cid)
            result = json.loads(path.read_text()) if path.exists() else {
                'schemaVersion': 1, 'conversationId': cid, 'selectedSnapshotId': '', 'artifacts': {}}
            if result.get('schemaVersion') != 1 or result.get('conversationId') != cid or not isinstance(result.get('artifacts'), dict):
                raise ValueError('事中派生档案格式不匹配，未覆盖原文件')
            snapshots = self._snapshots(cid)
            # Recover existing runtime archives without rewriting trajectories or requiring a live engine.
            for path in self.runs_root.glob('run-*/manifest.json'):
                try:
                    manifest = json.loads(path.read_text())
                    source = (manifest.get('class2_demo') or manifest.get('scenario', {}).get('class2_demo') or {}).get('source', {})
                    sid = source.get('snapshotId')
                    if source.get('conversationId') != cid or sid not in snapshots or snapshot_id and sid != snapshot_id:
                        continue
                    run = self._run(cid, sid, path.parent.name)
                    result['artifacts'].setdefault(run['runId'], {'id': run['runId'], 'kind': 'run',
                        'snapshotId': sid, 'runId': run['runId'], 'revision': 0, 'payload': run})
                except (OSError, ValueError, TypeError):
                    continue  # A damaged archive is never guessed or assigned to another plan.
            if snapshot_id:
                result['artifacts'] = {k: v for k, v in result['artifacts'].items() if v['snapshotId'] == snapshot_id}
            return result

    def save(self, cid, body):
        with self._lock, self.requirements._lock:
            path = self._path(cid)
            snapshots = self._snapshots(cid)
            sid = body.get('snapshotId')
            if sid not in snapshots:
                raise ValueError('找不到此会话的已保存方案快照')
            record = json.loads(path.read_text()) if path.exists() else {
                'schemaVersion': 1, 'conversationId': cid, 'selectedSnapshotId': '', 'artifacts': {}}
            if record.get('schemaVersion') != 1 or record.get('conversationId') != cid or not isinstance(record.get('artifacts'), dict):
                raise ValueError('事中派生档案格式不匹配，未覆盖原文件')
            if body.get('action') == 'select_plan':
                record['selectedSnapshotId'] = sid
            else:
                kind, payload = body.get('kind'), deepcopy(body.get('payload'))
                if kind not in {'run', 'scene', 'generation', 'analysis', 'report', 'legacy'} or not isinstance(payload, dict):
                    raise ValueError('无效的事中派生记录')
                run_id = body.get('runId')
                scene_id = body.get('sceneId')
                snapshot_generation = kind == 'generation' and run_id is None
                if snapshot_generation:
                    if snapshots[sid].get('requirement', {}).get('research', {}).get('problem_type') != 2:
                        raise ValueError('候选来源不是第二类方案')
                    if scene_id is not None:
                        raise ValueError('方案候选不能绑定运行现场')
                    origin = payload.get('sceneIdentity')
                    source = json.loads(origin) if isinstance(origin, str) else None
                    if (not isinstance(source, list) or len(source) != 7 or source[0] != sid
                            or not isinstance(source[1], str) or not RUN_ID.fullmatch(source[1])):
                        raise ValueError('候选的生成现场不属于当前方案')
                else:
                    run = self._run(cid, sid, run_id)
                identity = body.get('id')
                if not isinstance(identity, str) or not CONVERSATION_ID.fullmatch(identity):
                    raise ValueError('派生记录必须有独立标识')
                artifacts = record['artifacts']
                if kind == 'run':
                    if identity != run_id:
                        raise ValueError('运行记录标识不匹配')
                    runtime = payload.get('runtime')
                    if runtime and (runtime.get('runId') != run_id or runtime.get('receipt', {}).get('requirement_id') != run['requirementId']):
                        raise ValueError('运行恢复指针与存档不一致')
                    payload = {**run, **({'runtime': runtime} if runtime else {})}
                elif kind == 'scene':
                    if (payload.get('snapshotId') != sid or payload.get('runId') != run_id
                            or payload.get('requirementId') != run['requirementId']
                            or payload.get('networkSha256') != run['networkSha256']
                            or not payload.get('capturedAt') or not payload.get('observations')):
                        raise ValueError('采样现场与运行存档不一致')
                else:
                    if kind in {'analysis', 'report'}:
                        parents = payload.get('sourceIds')
                        if not isinstance(parents, list) or not parents or any(not isinstance(parent, str) or artifacts.get(parent, {}).get('snapshotId') != sid or artifacts.get(parent, {}).get('runId') != run_id for parent in parents):
                            raise ValueError('派生产物缺少同一方案和运行的来源记录')
                    scene = artifacts.get(scene_id, {})
                    if kind == 'generation' and not snapshot_generation and (scene.get('kind') != 'scene' or scene.get('runId') != run_id or scene.get('snapshotId') != sid):
                        raise ValueError('决策缺少对应现场记录')
                    if kind == 'generation':
                        options = payload.get('options')
                        if not isinstance(options, list) or not options or any(not isinstance(o, dict) or not isinstance(o.get('text'), str) or not o['text'].strip() or not isinstance(o.get('id'), str) for o in options) or len({o['id'] for o in options}) != len(options):
                            raise ValueError('候选决策记录不完整')
                        if payload.get('selectedStrategyId') and payload['selectedStrategyId'] not in {f"llm-{o['id']}" for o in options}:
                            raise ValueError('选择结果不属于当前候选批次')
                        if snapshot_generation:
                            choices = payload.get('selectionsByRun', {})
                            option_ids = {f"llm-{o['id']}" for o in options}
                            if not isinstance(choices, dict) or any(not RUN_ID.fullmatch(key) or not isinstance(value, str) or value not in option_ids for key, value in choices.items()):
                                raise ValueError('运行选择记录无效')
                        if not isinstance(payload.get('messages'), list):
                            raise ValueError('决策对话记录无效')
                previous = artifacts.get(identity)
                if previous:
                    old_origin = previous['payload'].get('sceneIdentity')
                    promotion = (snapshot_generation and previous.get('kind') == kind and previous.get('snapshotId') == sid
                                 and previous.get('runId') and source[1] == previous['runId'] and (not old_origin or old_origin == origin))
                    if not promotion and any(previous.get(k) != v for k, v in {'kind': kind, 'snapshotId': sid, 'runId': run_id, 'sceneId': scene_id}.items()):
                        raise ValueError('记录标识已绑定其他来源')
                    if snapshot_generation and not promotion and old_origin != origin:
                        raise ValueError('候选的生成来源不可覆盖')
                    if not promotion and previous['payload'] == payload:
                        return previous
                    if kind != 'run' and body.get('expectedRevision') != previous['revision']:
                        raise ArtifactConflict('派生记录已有更新，保留本地内容，请重新载入后再修改')
                    if kind in {'scene', 'legacy'} or kind == 'generation' and (previous['payload']['options'] != payload['options'] or previous['payload'].get('generatedAt') != payload.get('generatedAt')):
                        raise ValueError('已保存的现场或候选正文不可覆盖，请另存新批次')
                elif body.get('expectedRevision', 0) != 0:
                    raise ArtifactConflict('原派生记录不存在，未覆盖其他记录')
                row = {'id': identity, 'kind': kind, 'snapshotId': sid, 'runId': run_id,
                       'sceneId': scene_id, 'revision': (previous or {}).get('revision', 0) + 1,
                       'updatedAt': datetime.now(timezone.utc).isoformat(), 'payload': payload,
                       'snapshotHash': _digest(snapshots[sid])}
                artifacts[identity] = row
                if not record['selectedSnapshotId']:
                    record['selectedSnapshotId'] = sid
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.root, delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(record, handle, ensure_ascii=False, allow_nan=False, indent=2)
                temporary.replace(path)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            return row if body.get('action') != 'select_plan' else record
