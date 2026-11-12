"""During-only workflow identity and durable, versioned page products."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from .repository import CONVERSATION_ID


def experiment_group_id(conversation_id):
    if not isinstance(conversation_id, str) or not CONVERSATION_ID.fullmatch(conversation_id):
        raise ValueError('无效的事中会话标识')
    return 'exp_' + hashlib.sha256(('during:' + conversation_id).encode()).hexdigest()[:32]


def confirmed_workflow(conversation_id, requirement):
    snapshots = requirement.get('snapshots') or []
    if len(snapshots) != 1:
        raise ValueError('一个事中会话必须且只能确认一份方案快照')
    snapshot = snapshots[0]
    problem_type = snapshot.get('requirement', {}).get('research', {}).get('problem_type')
    if problem_type not in (1, 2):
        raise ValueError('事中工作流仅支持第一类或第二类问题')
    if requirement.get('draft', {}).get('requirement') != snapshot.get('requirement'):
        raise ValueError('确认快照与当前需求清单不一致')
    if not isinstance(snapshot.get('modeling_requirements'), dict):
        raise ValueError('确认快照缺少人工社会建模需求')
    return {'conversationId': conversation_id, 'problemType': problem_type,
            'snapshotId': snapshot['id'], 'experimentId': experiment_group_id(conversation_id),
            'lockedAt': snapshot.get('confirmedAt'), 'state': 'confirmed'}


def preserve_confirmed_content(previous, incoming):
    # Chat and tool receipts can grow; research and modeling inputs are immutable.
    for field in ('snapshots', 'modelingRequirements'):
        if previous.get(field) != incoming.get(field):
            raise ValueError('方案已确认锁定，不能修改需求或人工社会建模配置；请新建会话')
    fields = ('id', 'revision', 'requirement', 'confirmation', 'savedSnapshotId')
    if any(previous['draft'].get(key) != incoming['draft'].get(key) for key in fields):
        raise ValueError('方案已确认锁定，不能修改需求；请新建会话')


def persisted_workspace_key(key):
    return isinstance(key, str) and (key.startswith('class2.') or key in {
        'crowdsim_during_workflow_instance', 'crowdsim_during_experiment_results',
        'crowdsim_during_evaluation_results', 'crowdsim_during_optimization_plan',
        'crowdsim_during_report_preview', 'crowdsim_during_llm_skill_results_v1',
        'crowdsim_backend_decision_evaluation_v3', 'crowdsim_event_decision_records',
        'crowdsim_active_triggered_event_v1', 'crowdsim_applied_triggered_strategy_v1',
        'crowdsim_during_event_decision_v1', 'crowdSim_lastReport',
    })


RESULT_KEYS = {'crowdsim_during_workflow_instance', 'crowdsim_during_experiment_results',
               'crowdsim_during_evaluation_results', 'crowdsim_during_optimization_plan',
               'crowdsim_during_report_preview', 'crowdsim_during_llm_skill_results_v1',
               'crowdsim_backend_decision_evaluation_v3', 'crowdSim_lastReport'}


class WorkspaceConflict(ValueError):
    def __init__(self, record):
        super().__init__('此会话的业务记录已有更新，请重新加载后重试')
        self.record = record


class DuringWorkflowRepository:
    def __init__(self, root, requirements, artifacts):
        self.root = Path(root)
        self.requirements, self.artifacts = requirements, artifacts
        self._lock = threading.RLock()

    def _path(self, cid):
        experiment_group_id(cid)
        return self.root / f'{cid}.json'

    def workspace(self, cid):
        with self._lock:
            path = self._path(cid)
            record = json.loads(path.read_text()) if path.exists() else {
                'conversationId': cid, 'values': {}, 'versions': {}, 'mutations': {}}
            if record.get('conversationId') != cid:
                raise ValueError('业务记录不属于此会话')
            version = self._evidence_version(cid, record)
            if record.get('evidenceVersion') != version:
                for key in RESULT_KEYS:
                    if key in record['values']:
                        record['values'].pop(key)
                        record['versions'][key] = record['versions'].get(key, 0) + 1
                record['evidenceVersion'] = version
            return record

    def _control(self, name):
        root = Path(os.environ.get('CROWDSIM_EXPERIMENT_DIR') or Path(__file__).resolve().parents[1] / 'runs/control_experiments')
        path = root / (name + '.json')
        return json.loads(path.read_text()) if path.exists() else []

    def _evidence_version(self, cid, record):
        gid = experiment_group_id(cid)
        group = next((row for row in self._control('experiments') if row.get('experiment_id') == gid), {})
        runs = sorted((row['run_id'], row.get('restartCount', 0)) for row in self._control('runs') if row.get('experiment_id') == gid)
        runtime = json.loads(record.get('values', {}).get('class2.runtime.v1') or '{}')
        comparison = self._comparison(cid)
        comparison_version = [comparison.get('executionVersion'), [(r['id'], r['attempt']) for r in comparison.get('runs', [])]] if comparison else None
        return hashlib.sha256(json.dumps([group.get('executionVersion', 0), runs, runtime.get('runId'), comparison_version]).encode()).hexdigest()

    def _comparison(self, cid):
        root = Path(os.environ.get('CROWDSIM_EXPERIMENT_DIR') or Path(__file__).resolve().parents[1] / 'runs/control_experiments')
        path = root / 'class2_comparisons' / (cid + '.json')
        comparison = json.loads(path.read_text()) if path.exists() else None
        if comparison and (comparison.get('conversationId') != cid or comparison.get('experimentId') != experiment_group_id(cid)):
            raise ValueError('平行实验不属于此会话的固定实验组')
        return comparison

    def load(self, cid):
        envelope = self.requirements.load(cid)
        if envelope is None:
            return None
        workflow = confirmed_workflow(cid, envelope['state']['requirement'])
        artifacts = self.artifacts.load(cid, workflow['snapshotId'])
        group = next((row for row in self._control('experiments') if row.get('experiment_id') == workflow['experimentId']), None)
        if group is None:
            group = {'experiment_id': workflow['experimentId'], 'conversationId': cid, 'status': 'draft',
                     'runs': [row['payload'] for row in artifacts['artifacts'].values() if row['kind'] == 'run']}
        comparison = self._comparison(cid)
        if comparison and comparison.get('snapshotId') != workflow['snapshotId']:
            raise ValueError('平行实验不属于当前确认快照')
        return {'schemaVersion': 1, **workflow, 'revision': envelope['revision'],
                'requirementRecord': envelope, 'workspace': self.workspace(cid),
                'experimentGroup': group, 'artifacts': artifacts,
                'comparison': {k: comparison[k] for k in ('experimentId', 'executionVersion', 'generationId', 'status')} if comparison else None}

    def save(self, cid, body):
        if not isinstance(body, dict):
            raise ValueError('工作流保存请求必须为对象')
        with self.requirements._lock, self._lock:
            envelope = self.requirements.load(cid)
            if envelope is None:
                raise ValueError('请先确认并保存本会话的需求快照')
            workflow = confirmed_workflow(cid, envelope['state']['requirement'])
            if body.get('snapshotId') != workflow['snapshotId']:
                raise ValueError('业务记录不属于本会话的确认快照')
            key, value = body.get('key'), body.get('value')
            mutation, expected = body.get('mutationId'), body.get('expectedRevision')
            if not persisted_workspace_key(key) or value is not None and not isinstance(value, str):
                raise ValueError('无效的事中业务记录')
            if not isinstance(mutation, str) or not CONVERSATION_ID.fullmatch(mutation) or type(expected) is not int or expected < 0:
                raise ValueError('业务记录缺少有效的操作版本')
            record = self.workspace(cid)
            if key in RESULT_KEYS and value is not None and body.get('evidenceVersion') != record['evidenceVersion']:
                raise WorkspaceConflict(record)
            digest = hashlib.sha256(json.dumps([key, value, body['snapshotId']], ensure_ascii=False).encode()).hexdigest()
            previous = record['mutations'].get(key)
            if previous and previous['id'] == mutation:
                if previous['hash'] != digest:
                    raise ValueError('同一操作标识不能用于不同内容')
                return record
            if record['versions'].get(key, 0) != expected:
                raise WorkspaceConflict(record)
            if value is not None and key.startswith('class2.'):
                parsed = json.loads(value)
                if not isinstance(parsed, dict):
                    raise ValueError('第二类产物必须为结构化对象')
                sid = parsed.get('snapshotId')
                source = (parsed.get('receipt') or {}).get('class2_demo', {}).get('source', {})
                if sid and sid != workflow['snapshotId'] or source and source != {'conversationId': cid, 'snapshotId': workflow['snapshotId']}:
                    raise ValueError('推演产物来源与会话不一致')
                current = json.loads(record['values'].get('class2.runtime.v1') or '{}').get('runId')
                if key == 'class2.runtime.v1' and parsed.get('runId'):
                    self.artifacts._run(cid, workflow['snapshotId'], parsed['runId'])
                if key in {'class2.scene.v1', 'class2.decision.v1'} and parsed.get('runId') != current:
                    raise ValueError('现场或决策不属于当前推演')
            record = deepcopy(record)
            if value is None:
                record['values'].pop(key, None)
            else:
                record['values'][key] = value
            record['versions'][key] = expected + 1
            record['mutations'][key] = {'id': mutation, 'hash': digest}
            if key == 'class2.runtime.v1':
                version = self._evidence_version(cid, record)
                if version != record['evidenceVersion']:
                    for product in RESULT_KEYS:
                        if product in record['values']:
                            record['values'].pop(product)
                            record['versions'][product] = record['versions'].get(product, 0) + 1
                    record['evidenceVersion'] = version
            record['snapshotId'] = workflow['snapshotId']
            record['updatedAt'] = datetime.now(timezone.utc).isoformat()
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.root, delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(record, handle, ensure_ascii=False, allow_nan=False)
                    handle.flush()
                temporary.replace(self._path(cid))
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            return record
