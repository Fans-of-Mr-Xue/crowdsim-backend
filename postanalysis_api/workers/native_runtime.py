"""Post-only bridge to the unmodified during SimulationRuntime.

The observation upload remains a calibration target, not a replay script.
Native actions are executed at simulation boundaries; UI batch controls never
enter the behavioral evidence stream.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

from postanalysis_api.schemas.common import ApiError

VERSION = 'post-during-runtime/1.0'


def changed_actions(left, right):
    """One removed/added action is one factor; otherwise compare input leaves."""
    def flatten(value, prefix):
        if isinstance(value, dict):
            return {key: item for name, child in value.items() for key, item in flatten(child, prefix + '.' + name).items()}
        return {prefix: value}
    a, b = {row['id']: row for row in left}, {row['id']: row for row in right}
    changes = sorted(a.keys() ^ b.keys())
    for key in sorted(a.keys() & b.keys()):
        x, y = flatten(a[key], key), flatten(b[key], key)
        changes.extend(name for name in sorted(x.keys() | y.keys()) if x.get(name) != y.get(name))
    return changes


def native_action(action):
    from crowdsim.domain.action_contract import validate_control_action
    if not isinstance(action, dict):
        raise ApiError('ACTION_PARAMETER_INVALID', '动作必须为对象', 422)
    if action.get('actionType') in {'open_gate', 'guidance'}:
        raise ApiError('NATIVE_ACTION_MAPPING_REQUIRED',
                       '旧通道开放/遵从比例/延迟方案须重新映射为事中原生动作；不能与新版F-00混算。', 422)
    try:
        normalized = validate_control_action({'actionId': action.get('id'), 'actionType': action.get('actionType'),
                                       'target': action.get('target', {}), 'parameters': action.get('parameters', {})})
        fields = {'observe_only': set(), 'publish_guidance': {'message', 'command', 'durationSeconds'},
                  'set_inflow_rate': {'rate', 'durationSeconds', 'endTimeSeconds'},
                  'set_edge_capacity': {'multiplier', 'durationSeconds'},
                  'set_edge_risk_weight': {'weight', 'durationSeconds'},
                  'reroute_group': {'routeEdges'}, 'set_route_distribution': {'distributions', 'durationSeconds'}}
        if set(normalized['parameters']) - fields[normalized['actionType']]:
            raise ValueError('动作包含未使用的参数，不能静默忽略')
        if normalized['actionType'] == 'publish_guidance' and normalized['parameters']['command'] != 'inform' and not normalized['target'].get('regionId'):
            raise ValueError('行为引导需要target.regionId关联活动或事件，才能进入个体响应链')
        if normalized['actionType'] == 'publish_guidance':
            import math
            target = normalized['target']
            coverage = [key for key in ('x', 'y', 'radius') if key in target]
            if coverage and (len(coverage) != 3 or any(type(target[key]) not in (int, float) or not math.isfinite(target[key]) for key in coverage) or target['radius'] <= 0):
                raise ValueError('引导覆盖的x、y和正数radius必须一起设置')
        return normalized
    except ValueError as exc:
        raise ApiError('ACTION_PARAMETER_INVALID', str(exc), 422) from exc


def validate_task(task, dataset, completed=()):
    from postanalysis_api.services.factual_context import is_spatial
    if not is_spatial(dataset):
        raise ApiError('FACTUAL_REIMPORT_REQUIRED', '新版事后执行链需要原空间观测及初始化分布，请重新导入事中归档。', 422)
    for group in task.get('groups', []):
        for action in group['interventions']:
            normalized = native_action(action)
            target = normalized['target']
            edge = target.get('entryId') or target.get('edgeId')
            if edge:
                from .memorial_scenario import network
                if not network().hasEdge(str(edge)):
                    raise ApiError('ACTION_TARGET_UNMAPPED', f'原生动作目标道路不存在：{edge}', 422)
        reference = next((other for other in task.get('groups', []) if other['id'] == group.get('referenceGroupId')), None)
        left = reference['interventions'] if reference else []
        changes = changed_actions(left, group['interventions'])
        if group.get('experimentType') == 'intervention' and len(changes) > 1:
            raise ApiError('INTERVENTION_NOT_ISOLATED', '单因素实验只能改变一个动作或一个动作参数。', 422)
        if group.get('experimentType') == 'mechanism':
            a, b = {row['id']: row for row in left}, {row['id']: row for row in group['interventions']}
            if not reference or len(changes) != 1 or len(a.keys() - b.keys()) != 1 or any(a.get(k) != row for k, row in b.items()):
                raise ApiError('MECHANISM_NOT_ISOLATED', '原生动作消融须参照完整策略组，仅删除一个动作。', 422)
    if any(run.get('executionProtocol') != VERSION for run in completed):
        raise ApiError('EXECUTION_PROTOCOL_MISMATCH', '旧执行链的已完成运行不能续入新版批次，请新建批次。', 409)


def compile_input(dataset, seed, actions, duration, path):
    """Keep spatial initialization, replace controller holds with native dwell.

    Compile without treatments so every world shares the same demand/profile
    inputs. Treatments may only affect the running simulation.
    """
    from .distribution_demand import compile_distribution
    from crowdsim.domain.person_parameters import (
        GOAL_LOCK_PARAM, HOTSPOT_ID_PARAM, HOTSPOT_TARGET_EDGE_PARAM, HOTSPOT_RELEASE_TIME_PARAM,
    )
    for action in actions:
        native_action(action)
    manifest = compile_distribution(dataset, seed, [], duration, path)
    tree = ET.parse(path)
    people = {person['id']: person for person in manifest['cohorts']}
    hotspot = dataset['metadata']['factualContext']['hotspot']['id']
    for node in tree.getroot().findall('person'):
        person = people[node.get('id')]
        for stop in list(node.findall('stop')):
            node.remove(stop)
        for key, value in ((GOAL_LOCK_PARAM, 'true'), (HOTSPOT_ID_PARAM, hotspot),
                           (HOTSPOT_TARGET_EDGE_PARAM, person['holdingEdge']),
                           (HOTSPOT_RELEASE_TIME_PARAM, person['release'])):
            ET.SubElement(node, 'param', key=key, value=str(value))
    tree.write(path, encoding='utf-8', xml_declaration=True)
    manifest.update(executionProtocol=VERSION, runtimeActions=actions,
                    treatmentAssignment='runtime_only', controlSource='no_additional_control_unless_explicit_native_actions')
    from .memorial_scenario import ROOT
    modules = ['crowdsim/core/simulation_runtime.py', 'crowdsim/decision/agent_decision.py',
               'crowdsim/decision/decision_scheduler.py', 'crowdsim/decision/plan_executor.py',
               'crowdsim/environment/information_model.py', 'crowdsim/core/state_updater.py',
               'crowdsim/modeling_algorithm/individual_model/pipeline.py']
    manifest['behaviorModuleSha256'] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in modules}
    manifest['assumptions'] = [note for note in manifest['assumptions'] if '引导' not in note] + [
        '事后复用当前事中规则决策、信息接收和动作执行；行为参数为当前模型配置，非历史实测',
        '新增原生动作只在运行时执行，未按预设遵从比例分配出口或提前释放人员',
        '消息送达/信任/动作执行为模型记录，不证明历史个体认知或事故因果']
    return manifest


class EvidenceWriter:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = self.path.open('w', encoding='utf-8')
        self.previous = {}
        self.counts = {'motion': 0, 'decision': 0, 'message': 0, 'intervention': 0}

    def write(self, kind, value):
        self.handle.write(json.dumps({'kind': kind, **value}, ensure_ascii=False, allow_nan=False) + '\n')
        self.counts[kind] += 1

    def motion(self, runtime):
        step = runtime.current
        current = {}
        for person_id, motion in step.persons.items():
            old = self.previous.get(person_id)
            dt = step.time_seconds - old[0] if old else None
            acceleration = (motion.speed - old[1]) / dt if dt and dt > 0 else None
            action = ('stopped' if motion.speed <= 1e-9 else 'accelerating' if acceleration is not None and acceleration > 1e-9
                      else 'decelerating' if acceleration is not None and acceleration < -1e-9 else 'moving')
            state = runtime.population.states[person_id]
            self.write('motion', {'timeSeconds': step.time_seconds, 'snapshotId': runtime.snapshot_id,
                'personId': person_id, 'x': motion.x, 'y': motion.y, 'edgeId': motion.edge_id,
                'speed': motion.speed, 'acceleration': acceleration, 'action': action,
                'knownEvents': state.known_events, 'activityState': state.activity_state,
                'plannedActivityHold': person_id in runtime.plan_executor.activity_hold_until,
                'inflowHold': person_id in runtime.plan_executor.flow_hold_reasons})
            current[person_id] = (step.time_seconds, motion.speed)
        self.previous = current

    def close(self):
        self.handle.close()


def execute(binary, routes, folder, extra_args, manifest, duration, cancelled, *, on_progress=None, before_step=None):
    """Same decision/message/action engine, fresh isolated runtime per seed."""
    from crowdsim.core.simulation_runtime import SimulationRuntime, RuntimeState
    from crowdsim.infrastructure.performance_probe import PerformanceOptions
    from .live_frames import capture_frame
    from .memorial_scenario import NET_FILE

    folder = Path(folder)
    config = ET.Element('configuration')
    inputs = ET.SubElement(config, 'input')
    ET.SubElement(inputs, 'net-file', value=str(NET_FILE))
    ET.SubElement(inputs, 'route-files', value=str(Path(routes).resolve()))
    clock = ET.SubElement(config, 'time')
    context = manifest['factualContext']
    ET.SubElement(clock, 'step-length', value=str(context['stepSeconds']))
    config_path = folder / 'bund.post.sumocfg'
    config_path.write_bytes(ET.tostring(config, encoding='utf-8', xml_declaration=True))

    class PostRuntime(SimulationRuntime):
        def apply_plan(self, plan):
            # Capture BEFORE the runtime mutates state.current_plan.
            pid = plan.person_id
            prior = {'profile': asdict(self.population.profile_for(pid)),
                     'state': asdict(self.population.states[pid]),
                     'observation': asdict(self.observations[pid]) if pid in self.observations else None,
                     'candidates': [asdict(item) for item in self.decision_candidates.get(pid, ())]}
            result = super().apply_plan(plan)
            evidence.write('decision', {'timeSeconds': self.time_seconds, 'snapshotId': self.snapshot_id,
                'personId': pid, 'context': prior, 'plan': asdict(plan), 'execution': asdict(result),
                'interpretation': 'model_decision_and_execution; not an estimated causal effect'})
            return result

    runtime = PostRuntime(config_path, pedestrian_route_files=[routes], sumo_binary=binary,
        extra_sumo_args=extra_args, demand_mode='fixed', timeline_end_seconds=duration,
        random_seed=manifest['seed'], performance_options=PerformanceOptions(enabled=False, system_enabled=False))
    sampler = runtime.population.profile_sampler
    sampler.config = context['populationProfiles']
    sampler.configure_distributions(context.get('populationDistributions', {}))
    # Same module classes as during; freeze per-run parameters in the manifest.
    decisions = context.get('decision', {})
    runtime.decision_scheduler.period_seconds = decisions.get('period_seconds', 2.0)
    runtime.decision_scheduler.llm_budget = decisions.get('llm_budget_per_tick', 12)
    runtime.decision_scheduler.concurrency = decisions.get('llm_concurrency', 4)
    runtime.demand_generation_report = {'initial_population': True, 'source': VERSION}
    actions = sorted(manifest.get('runtimeActions', []), key=lambda row: row['atSeconds'])
    index = message_index = 0
    last_publish = float('-inf')
    records, measurement_events, held = [], [], set()
    evidence = EvidenceWriter(folder / 'behavior_evidence.jsonl')
    try:
        runtime.initialize()
        runtime.recorder.update_manifest(postanalysis={
            'executionProtocol': VERSION, 'runtimeActions': actions,
            'behaviorModuleSha256': manifest['behaviorModuleSha256'],
            'configurationSource': context.get('configurationSource'),
            'historicalConfigurationArchived': context.get('historicalConfigurationArchived', False)})
        runtime.start()
        evidence.motion(runtime)
        while runtime.state == RuntimeState.RUNNING:
            if before_step:
                before_step()
            if cancelled():
                raise InterruptedError()
            while index < len(actions) and actions[index]['atSeconds'] <= runtime.time_seconds + 1e-9:
                action = native_action(actions[index])
                record = runtime.apply_control_action(action)
                evidence.write('intervention', {'timeSeconds': runtime.time_seconds,
                                               'scheduledSeconds': actions[index]['atSeconds'], 'action': action, 'result': record})
                records.append(record)
                index += 1
            runtime.tick()
            current_held = set(runtime.plan_executor.activity_hold_until) | set(runtime.plan_executor.flow_hold_reasons)
            for pid in sorted(current_held - held):
                measurement_events.append({'timeSeconds': runtime.time_seconds, 'personId': pid, 'type': 'hold'})
            for pid in sorted(held - current_held):
                measurement_events.append({'timeSeconds': runtime.time_seconds, 'personId': pid, 'type': 'release'})
            held = current_held
            for message in runtime.information.records[message_index:]:
                evidence.write('message', asdict(message))
            message_index = len(runtime.information.records)
            evidence.motion(runtime)
            if on_progress and (time.monotonic() - last_publish >= .5 or runtime.state == RuntimeState.FINISHED):
                try:
                    update = {'stepSeconds': runtime.time_seconds, 'frame': capture_frame(runtime.adapter.connection, runtime.time_seconds)}
                except Exception:
                    update = {'stepSeconds': runtime.time_seconds, 'frame': None, 'frameError': 'SUMO_LIVE_FRAME_UNAVAILABLE'}
                on_progress(update)
                last_publish = time.monotonic()
        directory = runtime.recorder.directory
        diagnostics = runtime.population.diagnostics()
    finally:
        try:
            runtime.close()  # Existing during cleanup closes SUMO and all runtime resources.
        finally:
            evidence.close()
            if runtime.recorder is not None:
                outcome = {'executionProtocol': VERSION, 'runtimeRunId': runtime.run_id,
                    'runtimeDirectory': str(runtime.recorder.directory), 'evidencePath': str(evidence.path),
                    'evidenceCounts': evidence.counts, 'population': runtime.population.diagnostics(),
                    'actionsExecuted': records, 'pendingActionCount': len(actions) - index,
                    'decisionDiagnostics': runtime.decision_diagnostics,
                    'runtimeCommand': [binary, '-c', str(config_path), '--start', '--quit-on-end', *runtime.adapter.extra_args]}
                (folder / 'runtime_outcome.json').write_text(json.dumps(outcome, ensure_ascii=False), encoding='utf-8')
    return {'executionProtocol': VERSION, 'runtimeRunId': runtime.run_id,
            'runtimeDirectory': str(directory), 'evidencePath': str(evidence.path),
            'evidenceCounts': evidence.counts, 'population': diagnostics,
            'actionsExecuted': records, 'pendingActionCount': len(actions) - index,
            'decisionDiagnostics': runtime.decision_diagnostics, 'measurementControlEvents': measurement_events,
            'runtimeCommand': outcome['runtimeCommand']}


def archive_evidence(repository, archive, outcome):
    """Stream artifacts to GridFS, never embed whole agent histories in BSON."""
    directory = Path(outcome['runtimeDirectory'])
    files = [Path(outcome['evidencePath']), *[directory / name for name in (
        'manifest.json', 'profiles.jsonl', 'messages.jsonl', 'lifecycle.jsonl', 'decision_manifest.json',
        'decisions.jsonl', 'decision_profiles.jsonl', 'decision_plans.jsonl', 'decision_routes.jsonl',
        'decision_person_ids.jsonl', 'decision_neighbors.jsonl', 'demand.rou.xml')]]
    artifacts, stored = [], []
    try:
        for path in files:
            if not path.is_file():
                raise OSError(f'行为证据文件缺失：{path.name}')
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            item = {'name': path.name, 'sizeBytes': path.stat().st_size, 'sha256': digest.hexdigest()}
            with path.open('rb') as handle:
                identifier = repository.files.upload_from_stream(path.name, handle, metadata={
                    'ownerId': archive['ownerId'], 'workspaceId': archive['workspaceId'],
                    'runId': archive['runId'], 'kind': 'post_behavior_evidence', 'executionProtocol': VERSION})
            stored.append(identifier)
            item['fileId'] = str(identifier)
            artifacts.append(item)
    except Exception:
        for identifier in stored:
            repository.files.delete(identifier)
        raise
    return {'version': VERSION, 'counts': outcome['evidenceCounts'], 'artifacts': artifacts,
            'runtimeRunId': outcome['runtimeRunId'], 'population': outcome['population'],
            'decisionDiagnostics': outcome['decisionDiagnostics'],
            'runtimeCommand': outcome.get('runtimeCommand'),
            'actionsExecuted': outcome['actionsExecuted'], 'pendingActionCount': outcome['pendingActionCount']}


def read_evidence(repository, run_id, user, workspace, name, offset=0, limit=65536):
    import base64
    from bson import ObjectId
    run = repository.get('runs', run_id, user, workspace)
    files = run.get('behaviorEvidence', {}).get('artifacts', [])
    item = next((item for item in files if item['name'] == name), None)
    if item is None:
        raise ApiError('NOT_FOUND', '此运行没有指定的行为证据文件。', 404)
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 262144:
        raise ApiError('INVALID_QUERY', '证据读取偏移或长度无效。', 400)
    with repository.files.open_download_stream(ObjectId(item['fileId'])) as handle:
        if offset > handle.length:
            raise ApiError('INVALID_QUERY', '读取偏移超过文件长度。', 400)
        handle.seek(offset)
        content = handle.read(limit)
    return {**item, 'runId': run_id, 'offset': offset, 'nextOffset': offset + len(content),
            'eof': offset + len(content) >= item['sizeBytes'], 'encoding': 'base64',
            'content': base64.b64encode(content).decode('ascii')}
