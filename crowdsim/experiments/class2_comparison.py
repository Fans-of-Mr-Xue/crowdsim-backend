"""Durable, single-engine comparisons of frozen class-two candidates.

The queue uses the same configure/start/pause commands and paced engine loop as a single run.
Completed attempts are immutable; restarting a service never resumes an engine.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import uuid

from dialog_storage.during_workflow import confirmed_workflow, experiment_group_id
from .during_observation import values_at, stats, METRICS
from .gateway_events import EventBus


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


class ComparisonConflict(ValueError):
    pass


class ComparisonStopped(Exception):
    pass


class Class2ComparisonService:
    def __init__(self, root, repository, gateway, requirements, artifacts, prepare, *, auto_start=True):
        self.root = Path(root)
        self.repository, self.gateway = repository, gateway
        self.requirements, self.artifacts, self.prepare = requirements, artifacts, prepare
        self.auto_start = auto_start
        self.owner = 'class2-comparison-' + uuid.uuid4().hex
        self._condition = threading.Condition(threading.RLock())
        self._thread = None
        self._active = None
        self._closing = False
        self._runtime_view = None
        self._watch = None
        self.events = EventBus(max_events=16)
        self.recover()
        self.gateway.add_listener(self._on_gateway_event)

    def _path(self, cid):
        experiment_group_id(cid)
        return self.root / (cid + '.json')

    def _read(self, cid):
        path = self._path(cid)
        record = json.loads(path.read_text()) if path.exists() else None
        if record is not None and (not isinstance(record, dict) or record.get('schemaVersion') != 1
                                   or record.get('conversationId') != cid
                                   or record.get('experimentId') != experiment_group_id(cid)):
            raise ValueError('平行实验档案来源或格式不匹配，原记录未覆盖')
        return record

    def _write(self, record, *, notify=True):
        self.root.mkdir(parents=True, exist_ok=True)
        record['updatedAt'] = now()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.root, delete=False) as f:
                temporary = Path(f.name)
                json.dump(record, f, ensure_ascii=False, allow_nan=False)
            temporary.replace(self._path(record['conversationId']))
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        if notify:
            self.events.publish('state', self.public(record), run_id=record['conversationId'])

    def recover(self):
        for path in self.root.glob('*.json'):
            record = self._read(path.stem)
            if record['status'] in {'queued', 'running', 'paused', 'pause_requested', 'stop_requested'}:
                record['status'] = 'interrupted'
                for row in record['runs']:
                    if row['status'] in {'preparing', 'running'}:
                        row.update(status='interrupted', error='实验服务已重启，本轮需要重新运行。')
                self._write(record)
                if record.get('engineOwner', '').startswith('class2-comparison-'):
                    self.repository.release_gateway_lease(record['engineOwner'])

    def _scope(self, user, workspace, cid):
        experiment_group_id(cid)
        if workspace != 'during-' + cid:
            raise ValueError('平行实验与会话工作区不一致')
        record = self._read(cid)
        if record and record['userId'] != user:
            raise ValueError('平行实验不属于当前用户')
        return record

    @staticmethod
    def public(record):
        if record is None:
            return None
        out = deepcopy(record)
        out.pop('executionRequest', None)
        out.pop('previous', None)
        for row in out['runs']:
            row.pop('attempts', None)
        out['summary'] = []
        for candidate in out['candidates']:
            completed = [r for r in out['runs'] if r['candidateId'] == candidate['id'] and r['status'] == 'completed']
            metrics = {key: {'label': METRICS[key][0], 'unit': METRICS[key][1],
                             **stats(r.get('metrics', {}).get(key, {}).get('mean') for r in completed)}
                       for key in out['metricIds'] if key in METRICS}
            out['summary'].append({'candidateId': candidate['id'], 'label': candidate['label'],
                                   'completedRuns': len(completed), 'requestedRuns': out['repetitions'], 'metrics': metrics})
        return out

    def load(self, user, workspace, cid):
        with self._condition:
            return self.public(self._scope(user, workspace, cid))

    def evaluation(self, user, workspace, cid, body=None):
        from .class2_evaluation import build_evaluation, finite
        with self._condition:
            record = self._scope(user, workspace, cid)
            if not record:
                raise ValueError('请先配置并运行本会话的第二类实验')
            envelope = self.requirements.load(cid)
            requirement = envelope['state']['requirement']
            workflow = confirmed_workflow(cid, requirement)
            if workflow['problemType'] != 2 or workflow['snapshotId'] != record['snapshotId']:
                raise ValueError('评估档案与本会话确认快照不一致')
            result = build_evaluation(record, self.root, requirement['snapshots'][0]['requirement'])
            path = self.root / cid / 'evaluation.json'
            saved = json.loads(path.read_text()) if path.exists() else None
            if body is not None:
                if body.get('conversationId') != cid or body.get('snapshotId') != result['snapshotId']:
                    raise ValueError('评估结果与当前会话或确认快照不一致')
                if body.get('fingerprint') != result['fingerprint']:
                    raise ComparisonConflict('实验数据已变化，请重新读取后再保存评估')
                ids = {c['id'] for c in result['candidates']}
                valid_ids = {r['candidateId'] for r in result['runs'] if r['valid']}
                if body.get('selectedCandidateId') not in valid_ids:
                    raise ValueError('请选择具有已完成实验数据的决策')
                reviews = body.get('reviews', {})
                if not isinstance(reviews, dict) or len(reviews) > len(ids)*4:
                    raise ValueError('无效的大模型评估结果')
                for key, review in reviews.items():
                    if (not isinstance(review, dict) or review.get('conversationId', cid) != cid
                            or review.get('snapshotId', result['snapshotId']) != result['snapshotId'] or review.get('candidateId') not in valid_ids
                            or review.get('skillId') not in {'strategy-alignment', 'risk-red-team', 'execution-readiness', 'multi-agent-review'}
                            or key != review['candidateId'] + '/' + review['skillId']
                            or review.get('fingerprint') != result['fingerprint'] or review.get('online') is not True
                            or not finite(review.get('total')) or not 0 <= review['total'] <= 100):
                        raise ValueError('大模型评估结果来源或格式不一致')
                reviews = {key: {**review, 'conversationId': cid, 'snapshotId': result['snapshotId']}
                           for key, review in reviews.items()}
                saved = {**result, 'reviews': deepcopy(reviews), 'selectedCandidateId': body['selectedCandidateId'],
                         'caseId': str(body.get('caseId') or '')[:120], 'savedAt': now()}
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
                        temporary = Path(handle.name)
                        json.dump(saved, handle, ensure_ascii=False, allow_nan=False)
                    temporary.replace(path)
                finally:
                    if temporary:
                        temporary.unlink(missing_ok=True)
            result['saved'] = ({k: saved.get(k) for k in ('fingerprint', 'reviews', 'selectedCandidateId', 'caseId', 'savedAt')}
                               if saved and saved.get('fingerprint') == result['fingerprint'] else None)
            result['savedOutdated'] = bool(saved and result['saved'] is None)
            return result

    @staticmethod
    def _new_runs(config):
        return [{'id': 'c2r-' + uuid.uuid4().hex, 'candidateId': candidate['id'],
                 'candidateLabel': candidate['label'], 'round': index + 1, 'seed': 20260908 + index,
                 'status': 'queued', 'attempt': 0, 'attempts': [], 'sampleCount': 0, 'metrics': {}}
                for candidate in config['candidates'] for index in range(config['repetitions'])]

    def _configuration(self, cid, body):
        envelope = self.requirements.load(cid)
        if not envelope:
            raise ValueError('请先保存本会话的确认快照')
        requirement = envelope['state']['requirement']
        workflow = confirmed_workflow(cid, requirement)
        if workflow['problemType'] != 2 or body.get('snapshotId') != workflow['snapshotId']:
            raise ValueError('平行实验仅支持本会话的第二类确认快照')
        generation_id = body.get('generationId')
        archive = self.artifacts.load(cid, workflow['snapshotId'])
        batch = archive['artifacts'].get(generation_id)
        if not batch or batch.get('kind') != 'generation' or batch['snapshotId'] != workflow['snapshotId']:
            raise ValueError('找不到已保存的候选决策批次')
        if batch['revision'] != body.get('generationRevision'):
            raise ComparisonConflict('候选决策已有更新，请完成保存后重新选择')
        generation = batch['payload']
        source = json.loads(generation.get('sceneIdentity') or 'null')
        if not isinstance(source, list) or len(source) != 7 or source[0] != workflow['snapshotId']:
            raise ValueError('候选决策缺少可信的采样来源')
        scene = next((a['payload'] for a in archive['artifacts'].values()
                      if a['kind'] == 'scene' and a['snapshotId'] == workflow['snapshotId']
                      and a.get('runId') == source[1] and a['payload'].get('capturedAt') == source[3]), None)
        context = (scene or {}).get('controlContext') or {}
        origin = context.get('source') or {}
        if (origin.get('conversationId') != cid or origin.get('snapshotId') != workflow['snapshotId']
                or origin.get('runId') != source[1] or scene.get('networkSha256') != source[6]
                or scene.get('runtimeState') not in {'READY', 'PAUSED'}):
            raise ValueError('候选决策缺少可复现的已初始化或暂停现场')
        selected = body.get('candidateIds')
        repeats = body.get('repetitions')
        if (not isinstance(selected, list) or not 1 <= len(selected) <= 10 or any(not isinstance(x, str) for x in selected)
                or len(set(selected)) != len(selected) or type(repeats) is not int or not 1 <= repeats <= 30
                or len(selected) * repeats > 100):
            raise ValueError('请选择决策，每种运行 1～30 次，总运行次数不超过 100 次')
        candidates = []
        for identity in selected:
            option = next((o for o in generation['options'] if o['id'] == identity), None)
            if not option or not option.get('actions'):
                raise ValueError('所选决策缺少已校验的可执行动作')
            if any(not isinstance(a, dict) for a in option['actions']):
                raise ValueError('所选决策动作格式无效')
            candidates.append({'id': identity, 'label': option.get('label') or identity,
                               'text': option.get('text', ''), 'actions': deepcopy(option['actions']),
                               'fingerprint': digest(option)})
        r = requirement['snapshots'][0]['requirement']
        design = deepcopy(r['class2']['flow_design'])
        design['flows'] = [{k: f[k] for k in ('id', 'name', 'count', 'density', 'speed', 'origin', 'destination')} for f in design['flows']]
        from crowdsim.scenarios.class2_flow_layout import flow_network
        request = {'problemType': 2, 'source': {'conversationId': cid, 'snapshotId': workflow['snapshotId']},
                   'count': r['population']['total'], 'phenomenon': r['class2'].get('phenomenon', {}).get('description') or r['project']['description'],
                   'requestedLocation': r['spatial_scope']['name'], 'projectTitle': r['project']['title'],
                   'durationSeconds': r['experiment']['duration_seconds'], 'populationDistributions': r['population']['distributions'],
                   'metricIds': r['observation']['metric_ids'], 'flowDesign': design, 'mapFingerprint': flow_network()['geometrySha256']}
        warmup = origin.get('timeSeconds')
        if type(warmup) not in (int, float) or not math.isfinite(warmup) or not 0 <= warmup < request['durationSeconds']:
            raise ValueError('候选决策采样时刻已超出实验窗口')
        for candidate in candidates:
            if any(type(a.get('startSeconds')) not in (int, float) or not math.isfinite(a['startSeconds'])
                   or a['startSeconds'] < warmup for a in candidate['actions']):
                raise ValueError('候选中存在早于采样时刻的动作，请重新生成')
        return {**workflow, 'generationId': generation_id, 'generationRevision': batch['revision'],
                'candidates': candidates, 'repetitions': repeats, 'executionRequest': request,
                'sourceRunId': source[1], 'networkSha256': source[6], 'warmupSeconds': warmup,
                'metricIds': request['metricIds']}

    def configure(self, user, workspace, cid, body):
        with self._condition:
            previous = self._scope(user, workspace, cid)
            token = body.get('clientRequestId')
            if not isinstance(token, str) or not 1 <= len(token) <= 120:
                raise ValueError('缺少有效的实验提交标识')
            payload_hash = digest(body)
            if previous and previous['clientRequestId'] == token:
                if previous['payloadHash'] != payload_hash:
                    raise ComparisonConflict('同一提交标识不能用于不同实验配置')
                return self.public(previous)
            version = previous['executionVersion'] if previous else 0
            if type(body.get('expectedVersion')) is not int or body['expectedVersion'] != version:
                raise ComparisonConflict('实验配置已有更新，请刷新后重试')
            if self._active == cid or previous and previous['status'] in {'queued', 'running', 'paused', 'pause_requested', 'stop_requested'}:
                raise ComparisonConflict('已有平行实验尚未结束')
            config = self._configuration(cid, body)
            record = {**config, 'schemaVersion': 1, 'userId': user, 'workspaceId': workspace, 'executionVersion': version + 1,
                      'clientRequestId': token, 'payloadHash': payload_hash, 'status': 'ready', 'createdAt': now(),
                      'previous': [*(previous or {}).get('previous', []), *([previous] if previous else [])], 'runs': []}
            # Avoid recursively copying the previous-history list into each archived execution.
            if previous:
                record['previous'][-1] = {k: v for k, v in previous.items() if k != 'previous'}
            record['runs'] = self._new_runs(config)
            self._write(record)
            return self.public(record)

    def start(self, user, workspace, cid, body):
        with self._condition:
            record = self._scope(user, workspace, cid)
            version = body.get('executionVersion')
            if record and type(version) is int and record.get('rerunOf') == version:
                return self.public(record)
            if not record or type(version) is not int or version != record['executionVersion']:
                raise ComparisonConflict('实验版本已变化，请刷新后重试')
            if self._active == cid and record['status'] in {'queued', 'running', 'paused', 'pause_requested'}:
                return self.public(record)
            if self._active or record['status'] not in {'ready', 'completed'}:
                raise ComparisonConflict('当前实验尚未就绪或已有平行实验正在运行')
            if record['status'] == 'completed':
                previous = {k: deepcopy(v) for k, v in record.items() if k != 'previous'}
                record.update(executionVersion=version + 1, rerunOf=version, error=None, createdAt=now(),
                              previous=[*record.get('previous', []), previous], runs=self._new_runs(record))
                self._runtime_view = None
            record['status'] = 'queued'
            self._write(record)
            self._launch(cid)
            return self.public(record)

    def _publish_runtime(self, cid, identity, *, init=None, frame=None, reset=False):
        with self._condition:
            record = self._read(cid)
            row = next(r for r in record['runs'] if r['id'] == identity)
            view = self._runtime_view
            if reset or not view or (view['conversationId'], view['executionVersion'], view['runId'], view['attempt']) != (cid, record['executionVersion'], identity, row['attempt']):
                view = {'conversationId': cid, 'snapshotId': record['snapshotId'], 'executionVersion': record['executionVersion'],
                        'runId': identity, 'attempt': row['attempt'], 'init': None, 'frame': None}
            if init is not None:
                view['init'] = deepcopy(init)
            if frame is not None:
                if not view['init'] or frame.get('run_id') != view['init']['run_id']:
                    raise RuntimeError('平行实验动态帧不属于当前轮次')
                if view['frame'] and frame['step_seconds'] < view['frame']['step_seconds']:
                    return
                view['frame'] = deepcopy(frame)
            self._runtime_view = view
            payload = {k: view[k] for k in ('conversationId', 'snapshotId', 'executionVersion', 'runId', 'attempt')}
            kind = 'frame' if frame is not None else 'init' if init is not None else 'reset'
            if kind != 'reset':
                payload[kind] = deepcopy(view[kind])
            self.events.publish(kind, payload, run_id=cid)
            self._condition.notify_all()

    def _on_gateway_event(self, payload):
        with self._condition:
            watch = self._watch
            if not watch:
                return
            if (payload.get('type') == 'gateway_state' and payload.get('state') in {'unavailable', 'disconnected'}
                    or payload.get('type') == 'runtime_error' and payload.get('run_id') == watch['runtimeId']):
                watch['error'] = payload.get('message') or payload.get('error') or '仿真连接已断开'
                self._condition.notify_all()
            elif payload.get('type') == 'update' and payload.get('run_id') == watch['runtimeId']:
                try:
                    record = self._read(watch['cid'])
                    row = next(r for r in record['runs'] if r['id'] == watch['identity'])
                    # Observe the normal engine stream once per native step.
                    # Map subscriptions and UI refresh frequency do not drive sampling.
                    if (watch['collecting'] and payload['step_seconds'] > record['warmupSeconds']
                            and payload['step_seconds'] > (row.get('lastObservation') or {}).get('timeSeconds', -1)):
                        self._sample(watch['cid'], watch['identity'], payload)
                    elif not watch['collecting']:
                        row['simulationTime'] = payload['step_seconds']
                        self._write(record, notify=False)
                    self._publish_runtime(watch['cid'], watch['identity'], frame=payload)
                except Exception as exc:
                    watch['error'] = str(exc)
                    self._condition.notify_all()

    def runtime(self, user, workspace, cid):
        with self._condition:
            record = self._scope(user, workspace, cid)
            if record is None:
                return None
            view = self._runtime_view
            row = next((r for r in record['runs'] if view and r['id'] == view.get('runId')), None)
            if (not view or view.get('conversationId') != cid or view.get('snapshotId') != record['snapshotId']
                    or view.get('executionVersion') != record['executionVersion'] or not row
                    or view.get('attempt') != row['attempt']
                    or view.get('init') and (view['init'].get('run_id') != row.get('runtimeRunId')
                        or view['init'].get('scenario', {}).get('class2_demo', {}).get('source') != {'conversationId': cid, 'snapshotId': record['snapshotId']})
                    or view.get('frame') and (not view.get('init') or view['frame'].get('run_id') != row.get('runtimeRunId'))):
                view = {}
            return {'record': self.public(record), 'conversationId': cid, 'snapshotId': record['snapshotId'],
                    'experimentId': record['experimentId'], 'executionVersion': record['executionVersion'],
                    'runId': view.get('runId', ''), 'attempt': view.get('attempt', 0),
                    'init': deepcopy(view.get('init')), 'frame': deepcopy(view.get('frame'))}

    def _launch(self, cid):
        if self._active:
            raise ComparisonConflict('已有平行实验占用执行队列')
        self._active = cid
        if self.auto_start:
            self._thread = threading.Thread(target=self.execute, args=(cid,), name='class2-comparison', daemon=True)
            self._thread.start()

    def control(self, user, workspace, cid, action, body):
        if action == 'set_speed':
            with self._condition:
                record = self._scope(user, workspace, cid)
                speed = body.get('speedFactor')
                if (not record or type(body.get('executionVersion')) is not int or body.get('executionVersion') != record['executionVersion']
                        or type(speed) not in (int, float) or not math.isfinite(speed) or not .05 <= speed <= 3):
                    raise ComparisonConflict('实验版本或速度无效')
                runtime_id = (self._watch or {}).get('runtimeId') if self._active == cid else None
            if runtime_id:
                self._command({'action': 'set_speed', 'run_id': runtime_id, 'speedFactor': speed}, 'speed')
            with self._condition:
                current = self._scope(user, workspace, cid)
                if current['executionVersion'] != body['executionVersion']:
                    raise ComparisonConflict('实验版本已变化，请刷新后重试')
                current['speedFactor'] = speed
                self._write(current)
            return self.load(user, workspace, cid)
        with self._condition:
            record = self._scope(user, workspace, cid)
            if not record or type(body.get('executionVersion')) is not int or body['executionVersion'] != record['executionVersion']:
                raise ComparisonConflict('实验版本已变化，请刷新后重试')
            status = record['status']
            if action == 'pause' and status in {'queued', 'running', 'pause_requested', 'paused'}:
                record['status'] = 'paused' if status == 'paused' else 'pause_requested'
            elif action == 'resume' and status in {'paused', 'pause_requested'} and self._active == cid:
                record['status'] = 'running'
            elif action == 'stop' and status in {'queued', 'running', 'paused', 'pause_requested', 'stop_requested'}:
                record['status'] = 'stop_requested'
            elif action == 'retry' and status in {'interrupted', 'completed_with_failures', 'stopped', 'failed'}:
                if self._active:
                    raise ComparisonConflict('已有平行实验占用执行队列')
                for row in record['runs']:
                    if row['status'] != 'completed':
                        row['attempts'].append({k: deepcopy(v) for k, v in row.items() if k != 'attempts'})
                        row.update(status='queued', sampleCount=0, metrics={}, error=None, runtimeRunId=None)
                        for field in ('firstSampleTime', 'lastObservation', 'simulationTime', 'actionReceipts',
                                      'installedPlanId', 'installReceipt', 'sampleArchive', 'completedAt'):
                            row.pop(field, None)
                record.update(status='queued', error=None)
                self._write(record)
                self._launch(cid)
                return self.public(record)
            else:
                raise ComparisonConflict('当前实验状态不支持此操作')
            self._write(record)
            self._condition.notify_all()
            return self.public(record)

    def _boundary(self, cid):
        with self._condition:
            while True:
                record = self._read(cid)
                if self._closing or record['status'] == 'stop_requested':
                    raise ComparisonStopped()
                if record['status'] == 'pause_requested':
                    record['status'] = 'paused'; self._write(record)
                if record['status'] != 'paused':
                    return
                if not self.repository.acquire_gateway_lease(self.owner, ttl_seconds=180):
                    raise RuntimeError('平行实验引擎占用已丢失')
                self._condition.wait(timeout=5)

    def _command(self, payload, expected, timeout=120):
        if self._closing:
            raise ComparisonStopped()
        if not self.repository.acquire_gateway_lease(self.owner, ttl_seconds=180):
            raise RuntimeError('SIMULATOR_BUSY')
        request = self.gateway.submit(payload)
        try:
            result = self.gateway.wait_for_ack(request, timeout=timeout)
        finally:
            self.gateway.forget_request(request)
        if not result or result.get('type') != expected or result.get('status') == 'rejected':
            raise RuntimeError((result or {}).get('message') or '仿真未返回匹配的执行确认')
        actual_run = result.get('run_id') or ((result.get('detail') or {}).get('source') or {}).get('runId')
        if payload.get('run_id') and actual_run != payload['run_id']:
            raise RuntimeError('仿真回执不属于当前运行')
        return result

    def _update_row(self, cid, identity, **updates):
        with self._condition:
            record = self._read(cid)
            row = next(r for r in record['runs'] if r['id'] == identity)
            row.update(deepcopy(updates))
            self._write(record)

    def _take_engine(self, record):
        status = self._command({'action': 'get_status'}, 'status', 20)
        state = str(status.get('state') or '').upper()
        if state in {'CREATED', 'CLOSED', 'ERROR'}:
            return
        run_id = status.get('run_id')
        source = ((status.get('scenario') or {}).get('class2_demo') or {}).get('source')
        owned = run_id == record['sourceRunId'] or any(r.get('runtimeRunId') == run_id for r in record['runs'])
        if (not owned or source != record['executionRequest']['source'] or state == 'RUNNING'):
            raise RuntimeError('仿真引擎仍被其他运行占用，请先在原推演中暂停或结束运行')
        frame = self._command({'action': 'get_state', 'run_id': run_id, 'includeClass2Control': True}, 'update')
        if run_id == record['sourceRunId'] and frame.get('class2_control'):
            raise RuntimeError('候选来源现场已有其他决策，需重新初始化并采样后生成对照决策')
        self._command({'action': 'close_run', 'run_id': run_id}, 'run_closed', 30)

    def _sample(self, cid, identity, frame):
        measured = (frame.get('metrics') or {}).get('observations')
        if not isinstance(measured, dict):
            raise RuntimeError('本轮缺少真实观测数据')
        with self._condition:
            record = self._read(cid)
            row = next(r for r in record['runs'] if r['id'] == identity)
            sample = {'timeSeconds': frame['step_seconds'], **{k: v for k, v in values_at(measured).items() if k in record['metricIds']}}
            flow_state = ((frame.get('metrics') or {}).get('class2_flows') or {}).get('evaluation')
            if isinstance(flow_state, dict):
                sample['flowState'] = deepcopy(flow_state)
            receipts = (frame.get('class2_control') or {}).get('actions', [])
            if receipts != row.get('actionReceipts', []):
                sample['actionUpdates'] = deepcopy(receipts)
            if sample['timeSeconds'] <= (row.get('lastObservation') or {}).get('timeSeconds', -1):
                raise RuntimeError('本轮观测时间没有递增')
            # Full engine observations remain in the runtime archive. Append the
            # comparison series separately instead of rewriting prior runs' data.
            path = self.root / cid / identity / f"attempt-{row['attempt']}.samples.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + '\n')
            row['sampleArchive'] = str(path.relative_to(self.root))
            row['sampleCount'] += 1
            row.setdefault('firstSampleTime', sample['timeSeconds'])
            row['lastObservation'] = sample
            for metric in record['metricIds']:
                value = sample.get(metric)
                if type(value) not in (float, int) or not math.isfinite(value):
                    continue
                old = row['metrics'].get(metric) or {'validSamples': 0, 'mean': 0}
                count = old['validSamples'] + 1
                row['metrics'][metric] = {'mean': old['mean'] + (value - old['mean']) / count,
                                          'min': min(old.get('min', value), value), 'max': max(old.get('max', value), value),
                                          'validSamples': count}
            row['simulationTime'] = frame['step_seconds']
            row['actionReceipts'] = (frame.get('class2_control') or {}).get('actions', [])
            self._write(record, notify=False)

    def _wait_for_run(self, cid, runtime_id, *, pause_at=None):
        renewed = time.monotonic()
        start_acknowledged = False
        while True:
            with self._condition:
                record = self._read(cid)
                watch = self._watch
                frame = (self._runtime_view or {}).get('frame') or {}
                if self._closing or record['status'] == 'stop_requested':
                    raise ComparisonStopped()
                if not watch or watch.get('error'):
                    raise RuntimeError((watch or {}).get('error') or '本轮运行监听已结束')
                status, engine_state = record['status'], frame.get('runtime_state')
            if time.monotonic() - renewed >= 30:
                if not self.repository.acquire_gateway_lease(self.owner, ttl_seconds=180):
                    raise RuntimeError('平行实验引擎占用已丢失')
                renewed = time.monotonic()
            if status == 'pause_requested':
                actual = self._command({'action': 'get_state', 'run_id': runtime_id, 'includeClass2Control': True}, 'update')
                if actual['runtime_state'] == 'RUNNING':
                    try:
                        self._command({'action': 'pause', 'run_id': runtime_id}, 'command_result')
                    except RuntimeError:
                        # The regular loop may have reached the loading boundary
                        # or finished between the read and the pause command.
                        actual = self._command({'action': 'get_state', 'run_id': runtime_id, 'includeClass2Control': True}, 'update')
                        if actual['runtime_state'] not in {'PAUSED', 'FINISHED'}:
                            raise
                with self._condition:
                    record = self._read(cid)
                    if record['status'] == 'pause_requested':
                        record['status'] = 'paused'; self._write(record)
                start_acknowledged = False
                continue
            if status == 'paused':
                with self._condition:
                    if self._read(cid)['status'] == 'paused' and not self._closing and not watch.get('error'):
                        self._condition.wait(timeout=5)
                continue
            if engine_state in {'ERROR', 'CLOSED'}:
                raise RuntimeError('本轮仿真异常结束')
            if pause_at is not None and frame.get('step_seconds', -1) >= pause_at - 1e-7:
                if engine_state != 'PAUSED' or abs(frame['step_seconds'] - pause_at) > 1e-7:
                    raise RuntimeError('本轮未在原决策采样时刻暂停')
                return frame
            if engine_state == 'FINISHED':
                if pause_at is not None:
                    raise RuntimeError('仿真在决策装载前结束')
                return frame
            if engine_state in {'READY', 'PAUSED'} and not start_acknowledged:
                self._command({'action': 'start', 'run_id': runtime_id,
                               **({'pauseAtSeconds': pause_at} if pause_at is not None else {})}, 'command_result')
                # The start ACK precedes the next update. Until an explicit pause,
                # an older display frame or a wakeup must not issue another start.
                start_acknowledged = True
            with self._condition:
                if ((self._runtime_view or {}).get('frame') is frame
                        and self._read(cid)['status'] == status and not self._closing and not watch.get('error')):
                    self._condition.wait(timeout=10)

    def _execute_row(self, cid, row):
        record = self._read(cid)
        self._update_row(cid, row['id'], status='preparing', attempt=row['attempt'] + 1, startedAt=now(), error=None)
        self._publish_runtime(cid, row['id'], reset=True)
        receipt = self.prepare({**record['executionRequest'], 'seed': row['seed']})
        self._boundary(cid)
        init = self._command({'action': 'configure', 'requirement_id': receipt['requirement_id'],
                              'count': record['executionRequest']['count'], 'speedFactor': record.get('speedFactor', 3), 'pushFps': 8}, 'init')
        runtime_id = init.get('run_id')
        binding = (init.get('scenario') or {}).get('class2_demo') or {}
        if (not runtime_id or binding.get('source') != record['executionRequest']['source']
                or binding.get('seed') != row['seed'] or binding.get('flow_design') != record['executionRequest']['flowDesign']
                or (init.get('scenario') or {}).get('network_sha256') != record['networkSha256']
                or (init.get('requirement') or {}).get('requirement_id') != receipt['requirement_id']
                or (init.get('scenario') or {}).get('class2_artificial_society') != receipt.get('class2_artificial_society')
                or not receipt.get('class2_artificial_society')):
            raise RuntimeError('本轮初始化与确认方案、种子或人工社会模型不一致')
        self._update_row(cid, row['id'], status='running', runtimeRunId=runtime_id,
                         requirementId=receipt['requirement_id'], artificialSociety=receipt['class2_artificial_society'],
                         archive=runtime_id, initialTime=init['step_seconds'])
        self._publish_runtime(cid, row['id'], init=init)
        with self._condition:
            self._watch = {'cid': cid, 'identity': row['id'], 'runtimeId': runtime_id, 'collecting': False, 'error': None}
        frame = self._command({'action': 'get_state', 'run_id': runtime_id, 'includeClass2Control': True}, 'update')
        self._publish_runtime(cid, row['id'], frame=frame)
        warmup = record['warmupSeconds']
        if frame['step_seconds'] < warmup - 1e-7:
            frame = self._wait_for_run(cid, runtime_id, pause_at=warmup)
        frame = self._command({'action': 'get_state', 'run_id': runtime_id, 'includeClass2Control': True}, 'update')
        if abs(frame['step_seconds'] - warmup) > 1e-7:
            raise RuntimeError('本轮无法准确重现候选决策的采样时刻')
        self._boundary(cid)
        candidate = next(c for c in record['candidates'] if c['id'] == row['candidateId'])
        source = (frame.get('class2_control_context') or {}).get('source')
        if not source or source.get('runId') != runtime_id:
            raise RuntimeError('本轮缺少可执行现场来源')
        plan = {'planId': row['id'] + ':' + str(row['attempt'] + 1), 'source': source, 'actions': candidate['actions']}
        installed = self._command({'action': 'install_class2_control_plan', 'run_id': runtime_id,
                                   'controlPlan': plan}, 'command_result')
        detail = installed.get('detail') or {}
        if (installed.get('status') != 'applied' or installed.get('action') != 'install_class2_control_plan'
                or detail.get('planId') != plan['planId'] or detail.get('source') != source):
            raise RuntimeError('本轮决策未被仿真确认装载')
        self._update_row(cid, row['id'], installedPlanId=plan['planId'], installReceipt=installed)
        with self._condition:
            self._watch['collecting'] = True
        frame = self._wait_for_run(cid, runtime_id)
        current = next(r for r in self._read(cid)['runs'] if r['id'] == row['id'])
        if not current['sampleCount']:
            raise RuntimeError('本轮没有装载决策后的有效观测')
        with self._condition:
            self._watch = None
        self._command({'action': 'close_run', 'run_id': runtime_id}, 'run_closed', 30)
        self._update_row(cid, row['id'], status='completed', completedAt=now())
        self._publish_runtime(cid, row['id'], frame=frame)

    def execute(self, cid):
        owned = False
        current_row = None
        try:
            if not self.repository.acquire_gateway_lease(self.owner, ttl_seconds=180):
                raise RuntimeError('SIMULATOR_BUSY')
            owned = True
            with self._condition:
                record = self._read(cid)
                record['engineOwner'] = self.owner
                self._write(record)
            if not self.gateway.start(wait_seconds=10):
                raise RuntimeError('CROWDSIM_UNAVAILABLE')
            self._boundary(cid)
            self._take_engine(self._read(cid))
            with self._condition:
                record = self._read(cid)
                if record['status'] == 'queued':
                    record['status'] = 'running'; self._write(record)
            for row in self._read(cid)['runs']:
                if row['status'] != 'queued':
                    continue
                self._boundary(cid)
                current_row = row
                try:
                    self._execute_row(cid, row)
                except ComparisonStopped:
                    raise
                except Exception as exc:
                    self._update_row(cid, row['id'], status='failed', error=str(exc), completedAt=now())
                    # A failed transport or reset is not a safe boundary for another candidate.
                    raise
                current_row = None
            with self._condition:
                record = self._read(cid)
                record['status'] = 'completed' if all(r['status'] == 'completed' for r in record['runs']) else 'completed_with_failures'
                self._write(record)
        except ComparisonStopped:
            with self._condition:
                record = self._read(cid)
                record['status'] = 'interrupted' if self._closing else 'stopped'
                for row in record['runs']:
                    if row['status'] in {'preparing', 'running', 'queued'}:
                        row['status'] = 'interrupted' if self._closing else 'cancelled'
                self._write(record)
        except Exception as exc:
            with self._condition:
                record = self._read(cid)
                record.update(status='failed', error=str(exc))
                self._write(record)
        finally:
            with self._condition:
                self._watch = None
            if owned:
                try:
                    record = self._read(cid)
                    row = next((r for r in record['runs'] if current_row and r['id'] == current_row['id']), None)
                    if row and row.get('runtimeRunId'):
                        self._command({'action': 'close_run', 'run_id': row['runtimeRunId']}, 'run_closed', 30)
                except Exception:
                    pass  # Pointer and error remain durable; never reset an unrelated run.
                self.gateway.close()
                self.repository.release_gateway_lease(self.owner)
            with self._condition:
                self._active = None
                self._condition.notify_all()

    def close(self):
        self.gateway.remove_listener(self._on_gateway_event)
        with self._condition:
            self._closing = True
            self._condition.notify_all()
