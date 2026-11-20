"""Source parity, archived-input integrity and the production analysis dispatcher."""
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import mongomock
import mongomock.gridfs

from algorithms._inference import paired_summary
from algorithms.mechanism_graph import compute_round_plays, compute_key_nodes
from postanalysis_api.repositories.mongodb import MongoRepository
from postanalysis_api.workers.analysis import summarize, paired_effect
from postanalysis_api.workers.source_analysis import complete_analysis, read_artifact
from postanalysis_api.workers.analysis_inputs import world_input
from postanalysis_api.workers.analysis_model import AnalysisModel, AnalysisCancelled


class RecordedModel:
    """Deterministic test-only responses following the source's real JSON guards."""
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, *, timeout):
        self.calls += 1
        if '"curve_summary"' in prompt:
            return {'curve_summary': '测试录制的曲线说明', 'key_points': []}
        if '"probabilities"' in prompt:
            return {'probabilities': {'stable': .4, 'congested': .4, 'unstable': .2, 'recovering': 0}, 'reason': '测试用录制分布'}
        if '"actions"' in prompt and '归并' in prompt:
            observations = json.loads(prompt.split('\n')[-1])
            return {'actions': [{'name': '实际移动与减速', 'explanation': '测试输入中的实际动作',
                'evidence': [{key: observations[0][key] for key in ('world_ref', 'tick_idx')}]}]}
        nodes = json.loads(prompt.split('本次实验词表：', 1)[1].split('\n', 1)[0])
        world = prompt.split('当前世界：', 1)[1].split('\n', 1)[0]
        action = next(node for node in nodes if node['tier'] == 'treatment')
        state = next(node for node in nodes if node.get('source_key') == 'agent_moving_samples')
        outcome = next(node for node in nodes if node['tier'] == 'outcome' and node['source_key'] == 'peak_density')
        return {'world_ref': world, 'relations': [
            {'source': action['id'], 'target': state['id'], 'relation': 'promotes', 'explanation': '录制测试的候选关系',
             'evidence': [{'tick_idx': 0, 'field': 'world_state.agent_moving_samples'}]},
            {'source': state['id'], 'target': outcome['id'], 'relation': 'promotes', 'explanation': '录制测试，待干预验证',
             'evidence': [{'tick_idx': None, 'field': 'rv_values.peak_density'}]}]}


def fixture():
    mongomock.gridfs.enable_gridfs_integration()
    repo = MongoRepository(mongomock.MongoClient().source_analysis)
    repo.files._timeout = None
    task = {'ownerId': 'u', 'workspaceId': 'w', 'datasetVersion': 'version-one', 'name': 'source test',
            'scenario': {'durationSeconds': 8}, 'metricIds': ['peak_density', 'mean_speed'], 'pcFeatures': [],
            'groups': [{'id': 'CF-01', 'name': '引导', 'hypothesis': '密度可能变化', 'experimentType': 'counterfactual',
                        'referenceGroupId': 'F-00', 'interventions': [{'id': 'g', 'actionType': 'publish_guidance', 'atSeconds': 1,
                            'target': {'regionId': 'crowd'}, 'parameters': {'message': '离开', 'command': 'disperse', 'durationSeconds': 5}}]}]}
    task = repo.create('experiments', 'u', 'w', task)
    runs = []
    for plan in ('F-00', 'CF-01'):
        for seed in (1, 2):
            run = repo.create('runs', 'u', 'w', {'experimentId': task['id'], 'datasetVersion': task['datasetVersion'],
                'planId': plan, 'seed': seed, 'status': 'completed', 'executionProtocol': 'post-during-runtime/1.0',
                'metrics': {'peak_density': seed + (1 if plan == 'F-00' else 0), 'mean_speed': .5},
                'timeline': [{'timeSeconds': i, 'population': 10, 'density': i / 3, 'meanSpeed': .5, 'pressureProxy': i / 10} for i in range(8)]})
            rows = []
            for i in range(8):
                rows.extend({'kind': 'motion', 'timeSeconds': i, 'personId': f'p{j}', 'action': 'moving'} for j in range(i + 1))
                rows.append({'kind': 'decision', 'timeSeconds': i, 'personId': 'p0', 'plan': {'proposed_action': 'slow_down', 'reason': 'crowding'},
                             'execution': {'status': 'applied'}, 'context': {'state': {'known_events': {}}}})
            encoded = ''.join(json.dumps(row) + '\n' for row in rows).encode()
            identifier = repo.files.upload_from_stream('behavior_evidence.jsonl', encoded,
                metadata={'ownerId': 'u', 'workspaceId': 'w', 'runId': run['id']})
            run['behaviorEvidence'] = {'artifacts': [{'name': 'behavior_evidence.jsonl', 'fileId': str(identifier),
                'sizeBytes': len(encoded), 'sha256': hashlib.sha256(encoded).hexdigest()}]}
            runs.append(run)
    return repo, task, runs


class SourceAnalysisTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('SUMO_BINARY'), 'set SUMO_BINARY for real SUMO batch verification')
    def test_real_batch_automatically_analyzes_and_serves_all_three_result_sections(self):
        from test_post_population_execution import population_dataset
        from postanalysis_api.workers.sumo_batch import SumoBatchExecutor
        from postanalysis_api.api.common import Request
        from postanalysis_api.api.results import handle
        from postanalysis_api.services.application import PostAnalysisApp
        repo, _, _ = fixture()
        data = population_dataset(3)
        task = repo.create('experiments', 'u', 'w', {
            'datasetId': data['id'], 'datasetVersion': data['version'], 'status': 'queued',
            'scenario': {'durationSeconds': 3}, 'seedStart': 42, 'metricIds': ['peak_density', 'mean_speed'],
            'pcFeatures': [], 'groups': [{'id': 'CF-01', 'name': 'guide', 'hypothesis': 'density may change',
                'experimentType': 'counterfactual', 'referenceGroupId': 'F-00', 'repeatCount': 1,
                'interventions': [{'id': 'guide', 'actionType': 'publish_guidance', 'atSeconds': 1,
                    'target': {'regionId': data['metadata']['factualContext']['hotspot']['id']},
                    'parameters': {'message': 'leave in order', 'command': 'disperse', 'durationSeconds': 2}}]}],
            'progress': {'baselinePlanned': 1, 'baselineCompleted': 0, 'interventionPlanned': 1,
                'interventionCompleted': 0, 'failed': 0, 'groups': [{'groupId': 'CF-01', 'planned': 1, 'completed': 0, 'failed': 0}]}})
        events = []
        def event(change):
            events.append(change)
            repo.update('experiments', task['id'], 'u', 'w', change)
        with TemporaryDirectory() as folder:
            executor = SumoBatchExecutor(repo, folder)
            executor.analysis_caller = RecordedModel()
            try:
                executor._run(task, data, event, threading.Event())
                self.assertEqual('completed', events[-1]['status'], events[-1])
                self.assertGreater(executor.analysis_caller.calls, 4)
            finally:
                executor.close()
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        app.repo = repo
        headers = {'X-CrowdSim-User': 'u', 'X-CrowdSim-Workspace': 'w'}
        for section in ('observation', 'intervention', 'mechanism'):
            status, body, _ = handle(Request('GET', f'/results/{task["id"]}/{section}', {}, {}, headers, app))
            self.assertEqual(200, status)
            self.assertIn('sourceAnalysis', body['data'])
        status, chunk, _ = handle(Request('GET', f'/results/{task["id"]}/analysis-artifact', {}, {}, headers, app))
        self.assertEqual(200, status)
        self.assertGreater(chunk['nextOffset'], 0)
        stored = repo.get('results', events[-1]['resultId'], 'u', 'w')
        self.assertEqual({'verified': 2}, stored['analysisPipeline']['behaviorEvidenceQuality'])
        self.assertEqual(2, stored['mechanism']['sourceAnalysis']['worldCount'])
        import traci
        self.assertFalse(traci.connection._connections)

    def test_pending_model_request_is_cancelled_without_a_background_thread(self):
        import asyncio
        cancel = threading.Event()
        ended = threading.Event()
        model = AnalysisModel.__new__(AnalysisModel)
        model.base, model.model, model.token = 'http://test.invalid/v1', 'test-model', ''
        model.calls, model.unavailable = 0, False
        def checkpoint():
            if cancel.is_set():
                raise AnalysisCancelled()
        model.checkpoint = checkpoint
        async def post(*args, **kwargs):
            cancel.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()
        client = SimpleNamespace(post=post)
        class Context:
            async def __aenter__(self):
                return client
            async def __aexit__(self, *args):
                pass
        with patch('httpx.AsyncClient', return_value=Context()):
            with self.assertRaises(AnalysisCancelled):
                model('test', timeout=60)
        self.assertTrue(ended.is_set())

    def test_shared_paired_ofat_and_ablation_cores_are_really_dispatched(self):
        _, _, runs = fixture()
        expected = paired_summary([2, 3], [1, 2], seed=20261023)
        for method, module in (('paired', 'paired_mc'), ('ofat', 'ofat'), ('ablation', 'mechanism_ablation')):
            effect = paired_effect(runs, 'F-00', 'CF-01', 'peak_density', method)
            self.assertEqual(expected['effect'], effect['estimate'])
            self.assertEqual(list(expected['ci']), effect['ci95'])
            self.assertIn(module, effect['algorithm'])

    def test_source_files_match_verified_import_hashes(self):
        root = Path(__file__).resolve().parents[1] / 'algorithms/powergame'
        manifest = json.loads((root / 'SOURCE.json').read_text(encoding='utf-8'))
        for name, reference in manifest['files'].items():
            self.assertEqual(reference['sha256'], hashlib.sha256((root / name).read_bytes()).hexdigest(), name)

    def test_forecasts_paths_actual_references_and_full_artifact_roundtrip(self):
        repo, task, runs = fixture()
        before = deepcopy(runs)
        result = complete_analysis(repo, task, runs, summarize(task, runs), caller=RecordedModel())
        self.assertEqual(before, runs)
        self.assertEqual('computed', result['analysisPipeline']['status'])
        self.assertEqual(4, result['mechanism']['sourceAnalysis']['worldCount'])
        forecast = result['observation']['sourceAnalysis']['forecasts']['groupTimelines'][0]
        self.assertEqual(0, forecast['points'][0]['timeSeconds'])
        self.assertEqual(2, forecast['points'][0]['validWorldCount'])
        self.assertEqual(0, forecast['points'][0]['probabilities']['recovering'])
        self.assertTrue(result['intervention']['sourceAnalysis']['paths'])
        contrast = result['intervention']['sourceAnalysis']['referenceContrasts'][0]
        self.assertEqual('F-00', contrast['referenceGroupId'])
        edge = next(edge for edge in contrast['edges'] if edge['unit'] == 'person/m2')
        self.assertEqual(-1, edge['score'])
        item = repo.create('results', 'u', 'w', {**result, 'datasetVersion': task['datasetVersion']})
        repo.update('experiments', task['id'], 'u', 'w', {'status': 'completed', 'resultId': item['id']})
        chunk = read_artifact(repo, task['id'], 'u', 'w')
        import base64
        full = json.loads(gzip.decompress(base64.b64decode(chunk['content'])))
        self.assertEqual(4, len(full['mechanism']['worlds']))
        self.assertEqual({run['id'] for run in runs}, {row['runId'] for row in full['worldBindings'].values()})
        self.assertFalse(full['intervention']['method']['causal_validation'])
        with self.assertRaises(Exception):
            read_artifact(repo, task['id'], 'other', 'w')

    def test_model_failure_keeps_numerical_results_and_never_invents_rule_paths(self):
        repo, task, runs = fixture()
        def offline(*args, **kwargs):
            raise ConnectionError('test offline')
        result = complete_analysis(repo, task, runs, summarize(task, runs), caller=offline)
        self.assertEqual('partial', result['analysisPipeline']['status'])
        self.assertEqual([], result['intervention']['sourceAnalysis']['paths'])
        self.assertEqual(-1, result['comparison']['effects'][0]['estimate'])

    def test_missing_response_metrics_remain_partial_without_failing_the_batch(self):
        repo, task, runs = fixture()
        for run in runs:
            run['metrics'] = {'peak_density': None, 'mean_speed': None}
        result = complete_analysis(repo, task, runs, summarize(task, runs), caller=RecordedModel())
        self.assertEqual('partial', result['analysisPipeline']['status'])
        self.assertEqual([], result['intervention']['sourceAnalysis']['paths'])
        self.assertIsNone(result['comparison']['effects'][0]['estimate'])

    def test_rejected_plans_never_become_executed_individual_actions(self):
        repo, task, runs = fixture()
        run = runs[0]
        encoded = b'{"kind":"decision","timeSeconds":1,"personId":"p","plan":{"proposed_action":"reroute"},"execution":{"status":"rejected"}}\n'
        identifier = repo.files.upload_from_stream('behavior_evidence.jsonl', encoded,
            metadata={'ownerId': 'u', 'workspaceId': 'w', 'runId': run['id']})
        run['behaviorEvidence']['artifacts'] = [{'name': 'behavior_evidence.jsonl', 'fileId': str(identifier),
            'sizeBytes': len(encoded), 'sha256': hashlib.sha256(encoded).hexdigest()}]
        world = world_input(repo, task, run, lambda: None)
        self.assertTrue(all(tick['decision']['action'] == '' for tick in world['ticks']))
        evidence = json.loads(world['ticks'][1]['scenario_content'])
        self.assertEqual({'reroute': 1}, evidence['proposedDecisionCounts'])
        self.assertEqual({'rejected': 1}, evidence['executionStatusCounts'])

    def test_window_prediction_clock_never_precedes_its_individual_evidence(self):
        repo, task, runs = fixture()
        rows = [{'kind': 'motion', 'timeSeconds': 1.9, 'personId': 'p', 'action': 'moving'}]
        run = runs[0]
        with patch('postanalysis_api.workers.analysis_inputs.evidence_rows', return_value=iter(rows)):
            world = world_input(repo, task, run, lambda: None)
        tick = world['ticks'][1]
        self.assertEqual(1.9, tick['timeSeconds'])
        self.assertEqual(1, json.loads(tick['scenario_content'])['macroObservationSeconds'])
        self.assertEqual(1.9, json.loads(tick['scenario_content'])['observedThroughSeconds'])
        run['timeline'] = [run['timeline'][0], run['timeline'][-1]]
        with patch('postanalysis_api.workers.analysis_inputs.evidence_rows', return_value=iter(rows)):
            world = world_input(repo, task, run, lambda: None)
        behavior_only = next(tick for tick in world['ticks'] if tick['tick_idx'] == 1)
        self.assertEqual(1, behavior_only['world_state']['agent_moving_samples'])
        self.assertNotIn('density', behavior_only['world_state'])
        self.assertIsNone(json.loads(behavior_only['scenario_content'])['macroObservationSeconds'])

    def test_tampered_individual_evidence_cannot_feed_macro_mechanism_counts(self):
        repo, task, runs = fixture()
        runs[0]['behaviorEvidence']['artifacts'][0]['sha256'] = '0' * 64
        world = world_input(repo, task, runs[0], lambda: None)
        self.assertEqual('invalid', world['behaviorEvidenceStatus'])
        self.assertTrue(all('agent_moving_samples' not in tick['world_state'] for tick in world['ticks']))

    def test_cancel_escapes_source_retry_loops_and_does_not_archive_results(self):
        repo, task, runs = fixture()
        with self.assertRaises(InterruptedError):
            complete_analysis(repo, task, runs, summarize(task, runs), cancelled=lambda: True, caller=RecordedModel())
        self.assertEqual(4, repo.db.postanalysis_files.files.count_documents({}))

    def test_round_numeric_branch_uses_supplied_weights_and_preserves_unplayed_rounds(self):
        graph = {'initialRisk': 2, 'microNodes': [{'id': 'p', 'name': 'agent p'}], 'microEdges': [], 'rounds': [
            {'id': 'r1', 'round': 1, 'name': '减速', 'threshold': 1.13, 'riskDelta': -.5,
             'drivers': [{'nodeId': 'p', 'effect': 1.125, 'weight': 1, 'lag': 1}]},
            {'id': 'r2', 'round': 2, 'name': '未发生动作', 'threshold': 2, 'riskDelta': 1,
             'drivers': [{'nodeId': 'p', 'effect': -1, 'weight': 1, 'lag': 0}]}]}
        plays = compute_round_plays(graph)
        self.assertTrue(plays[0]['played'])
        self.assertFalse(plays[1]['played'])
        self.assertEqual(1.5, plays[1]['riskAfter'])
        self.assertGreater(compute_key_nodes(graph)[0]['score'], 0)


if __name__ == '__main__':
    unittest.main()
