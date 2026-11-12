"""Code-level repository checks using temporary files only; no listeners/engines/models."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from dialog_storage.during_artifacts import DuringArtifactRepository, ArtifactConflict
from dialog_storage.during_requirements import DuringRequirementRepository
from crowdsim.scenarios.class2_society import publish_class2_society


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.requirements = DuringRequirementRepository(self.root / 'requirements-state')
        self.sid = 'snapshot-one'
        requirement = {'schema_version': 2, 'research': {'problem_type': 2}, 'population': {'total': 100, 'distributions': {}},
                       'experiment': {'duration_seconds': 300}, 'observation': {'metric_ids': ['crowd_density']},
                       'class2': {'flow_design': {'network_sha256': 'network', 'flows': []}}}
        self.state = {'requirement': {'version': 3, 'draft': {'id': 'draft', 'revision': 1, 'requirement': requirement},
                                    'snapshots': [{'id': self.sid, 'requirement': deepcopy(requirement), 'modeling_requirements': {}}], 'submissions': {}}}
        self.requirements.save('study', state=self.state, expected_revision=0, mutation_id='initial')
        self.run = 'run-' + 'a' * 32
        self.req = 'req-' + 'b' * 32
        self.runs = self.root / 'runs'
        (self.runs / self.run).mkdir(parents=True)
        (self.runs / 'requirements').mkdir()
        binding = {'source': {'conversationId': 'study', 'snapshotId': self.sid}, 'count': 100, 'duration_seconds': 300,
                   'flow_design': deepcopy(requirement['class2']['flow_design']), 'layout_sha256': 'layout'}
        self.manifest = self.runs / self.run / 'manifest.json'
        self.manifest.write_text(json.dumps({'run_id': self.run, 'class2_demo': binding, 'requirement': {'requirement_id': self.req}, 'scenario': {'network_sha256': 'network'}}))
        receipt = {'requirement_id': self.req, 'fingerprint': 'sha256:test', 'class2_demo': binding,
                   'requirement': {**deepcopy(requirement), 'class2_demo': binding}}
        publish_class2_society(receipt, requirements=self.requirements, root=self.runs)
        (self.runs / 'requirements' / (self.req + '.json')).write_text(json.dumps(receipt))
        self.repo = DuringArtifactRepository(self.root / 'derived', self.requirements, self.runs)
        self.scene = {'snapshotId': self.sid, 'runId': self.run, 'requirementId': self.req, 'networkSha256': 'network',
                      'step': 60, 'capturedAt': '2026-11-05T00:00:00Z', 'observations': {'time_seconds': 60}}
        self.put('scene', 'scene-one', self.scene)

    def put(self, kind, identity, payload, **extra):
        return self.repo.save('study', {'kind': kind, 'id': identity, 'snapshotId': self.sid, 'runId': self.run,
                                       'payload': payload, **extra})

    def generation(self):
        return {'options': [{'id': 'a', 'text': '真实返回的候选甲'}, {'id': 'b', 'text': '真实返回的候选乙'}],
                'messages': [], 'selectedStrategyId': '', 'generatedAt': '2026-11-05T01:00:00Z'}

    def test_unselected_generation_survives_restart_and_archive_is_unchanged(self):
        before = self.manifest.read_bytes()
        saved = self.put('generation', 'generation-one', self.generation(), sceneId='scene-one')
        restarted = DuringArtifactRepository(self.repo.root, self.requirements, self.runs)
        record = restarted.load('study', self.sid)
        self.assertEqual(record['artifacts'][saved['id']]['payload']['selectedStrategyId'], '')
        self.assertEqual(len(record['artifacts'][saved['id']]['payload']['options']), 2)
        self.assertIn(self.run, record['artifacts'])
        self.assertEqual(before, self.manifest.read_bytes())

    def test_batches_are_distinct_and_original_candidates_cannot_be_overwritten(self):
        old = self.put('generation', 'generation-one', self.generation(), sceneId='scene-one')
        newer = self.generation(); newer['generatedAt'] = 'later'
        self.put('generation', 'generation-two', newer, sceneId='scene-one')
        edited = self.generation(); edited['options'][0]['text'] = 'changed'
        with self.assertRaises(ValueError):
            self.put('generation', old['id'], edited, sceneId='scene-one', expectedRevision=1)
        self.assertIn('generation-two', self.repo.load('study')['artifacts'])
        self.assertEqual(self.repo.load('study')['artifacts'][old['id']], old)

    def test_selection_cas_and_idempotent_retry(self):
        old = self.put('generation', 'generation-one', self.generation(), sceneId='scene-one')
        self.assertEqual(old, self.put('generation', 'generation-one', self.generation(), sceneId='scene-one'))
        selected = self.generation(); selected['selectedStrategyId'] = 'llm-a'
        self.put('generation', old['id'], selected, sceneId='scene-one', expectedRevision=1)
        selected['selectedStrategyId'] = 'llm-b'
        with self.assertRaises(ArtifactConflict):
            self.put('generation', old['id'], selected, sceneId='scene-one', expectedRevision=1)

    def test_legacy_candidates_move_to_the_snapshot_without_a_live_or_archived_run(self):
        old = self.generation()
        old['selectedStrategyId'] = 'llm-a'
        self.put('generation', 'reusable', old, sceneId='scene-one')
        self.manifest.unlink()
        new_run = 'run-' + 'c' * 32
        payload = deepcopy(old)
        del payload['selectedStrategyId']
        payload['sceneIdentity'] = json.dumps([self.sid, self.run, self.req, self.scene['capturedAt'], 60, 'PAUSED', 'network'])
        payload['selectionsByRun'] = {self.run: 'llm-a', new_run: 'llm-b'}
        saved = self.put('generation', 'reusable', payload, runId=None, sceneId=None, expectedRevision=1)
        self.assertIsNone(saved['runId'])
        self.assertIsNone(saved['sceneId'])
        self.assertEqual(saved['payload']['options'], old['options'])
        self.assertEqual(self.repo.load('study', self.sid)['artifacts']['reusable']['payload']['selectionsByRun'], payload['selectionsByRun'])
        self.assertEqual(self.repo.load('study', self.sid)['artifacts']['scene-one']['runId'], self.run)
        payload['sceneIdentity'] = json.dumps(['other-snapshot', self.run, self.req, None, 0, 'READY', 'network'])
        with self.assertRaises(ValueError):
            self.put('generation', 'foreign-origin', payload, runId=None)

    def test_wrong_plan_scene_and_run_cannot_mix(self):
        with self.assertRaises(ValueError):
            self.put('generation', 'bad', self.generation(), sceneId='missing')
        with self.assertRaises(ValueError):
            self.repo.save('another-study', {'id': 'bad', 'kind': 'scene', 'snapshotId': self.sid, 'runId': self.run, 'payload': self.scene})
        bad = deepcopy(self.scene); bad['requirementId'] = 'other'
        with self.assertRaises(ValueError): self.put('scene', 'bad', bad)

    def test_model_bindings_cannot_be_deleted_by_a_new_draft(self):
        state = deepcopy(self.state)
        state['requirement'].update(artificialSocietyPublications={self.sid: {'model_id': 'model'}}, executionManifests={self.sid: {'status': 'READY'}})
        self.requirements.save('study', state=state, expected_revision=1, mutation_id='bindings')
        state['requirement']['artificialSocietyPublications'] = {}
        with self.assertRaises(ValueError):
            self.requirements.save('study', state=state, expected_revision=2, mutation_id='bad-reset')


if __name__ == '__main__':
    unittest.main()
