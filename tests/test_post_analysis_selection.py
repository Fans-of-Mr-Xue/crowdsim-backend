"""Names and optional knowledge remain bound to the selected completed batch."""
import unittest
import mongomock
from postanalysis_api.repositories.mongodb import MongoRepository
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.services.analysis_knowledge import read_context


class PostAnalysisSelectionTests(unittest.TestCase):
    def setUp(self):
        self.repo = MongoRepository.__new__(MongoRepository)
        self.repo.db = mongomock.MongoClient().db
        self.repo._indexes_ready = True
        self.task = {'id': 'exp_one', 'ownerId': 'u', 'workspaceId': 'post-other', 'datasetId': 'ds',
                     'datasetVersion': 'v1', 'createdAt': '2026-11-12T16:05:00Z', 'status': 'completed',
                     'resultId': 'r1', 'groups': [{'id': 'CF-01'}, {'id': 'CF-02'}]}
        self.repo.db.postanalysis_experiments.insert_one(dict(self.task))
        self.repo.db.postanalysis_datasets.insert_one({'id': 'ds', 'ownerId': 'u', 'workspaceId': 'post-other',
                                                       'metadata': {'sourceBinding': {'sourcePhase': 'during'}}})

    def test_history_and_detail_render_same_beijing_date_group_count_and_verified_source(self):
        detail = self.repo.get('experiments', 'exp_one', 'u', 'post-other')
        history = self.repo.experiment_history('u')['items'][0]
        expected = '2026年11月13日00点05分-2组反事实-基于事中数据'
        self.assertEqual(expected, detail['name'])
        self.assertEqual(expected, history['name'])
        self.assertNotIn('groups', history)
        self.repo.db.postanalysis_datasets.update_one({'id': 'ds'}, {'$set': {'originalFile': {'fileId': 'f'}}, '$unset': {'metadata': ''}})
        self.assertTrue(self.repo.get('experiments', 'exp_one', 'u', 'post-other')['name'].endswith('基于上传数据'))
        self.assertNotIn('name', self.repo.db.postanalysis_experiments.find_one({'id': 'exp_one'}))

    def test_only_requested_existing_libraries_are_used_and_deleted_or_unparsed_sources_are_ignored(self):
        self.repo.db.case_profiles.insert_many([{'_id': 'c1', 'name': 'case', 'summary': 'observed mechanism'},
                                               {'_id': 'deleted', 'name': 'deleted', 'is_deleted': True}])
        self.repo.db.emergency_plans.insert_many([{'_id': 'p1', 'name': 'plan', 'parse_status': 'parsed', 'core_actions': 'guidance'},
                                                 {'_id': 'p2', 'name': 'queued', 'parse_status': 'queued'}])
        empty = read_context(self.repo, 'exp_one', 'u', 'post-other', [])
        self.assertEqual([], empty['sources'])
        cases = read_context(self.repo, 'exp_one', 'u', 'post-other', ['cases'])
        self.assertEqual(['c1'], [item['id'] for item in cases['sources']])
        both = read_context(self.repo, 'exp_one', 'u', 'post-other', ['cases', 'plans'])
        self.assertEqual(['c1', 'p1'], [item['id'] for item in both['sources']])
        self.assertEqual('guidance', both['sources'][1]['notes']['intervention'])
        self.assertEqual('v1', both['datasetVersion'])

    def test_scope_state_and_library_validation(self):
        for user, workspace, libraries in [('other', 'post-other', []), ('u', 'wrong', []), ('u', 'post-other', ['unknown'])]:
            with self.assertRaises(ApiError):
                read_context(self.repo, 'exp_one', user, workspace, libraries)
        self.repo.db.postanalysis_experiments.update_one({'id': 'exp_one'}, {'$set': {'status': 'running'}})
        with self.assertRaises(ApiError):
            read_context(self.repo, 'exp_one', 'u', 'post-other', ['cases'])
