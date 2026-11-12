"""Owner-wide post history retains every status and original conversation scope."""
import json
from types import SimpleNamespace
import unittest

import mongomock

from postanalysis_api.api.common import Request
from postanalysis_api.api.experiments import handle
from postanalysis_api.repositories.mongodb import MongoRepository
from postanalysis_api.schemas.common import ApiError


class PostExperimentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().db
        self.repo = MongoRepository.__new__(MongoRepository)
        self.repo.db = self.db
        self.repo._indexes_ready = True

    def add(self, identity, *, owner='u', conversation='c1', status='completed', created='2026-11-12T10:00:00Z'):
        self.db.postanalysis_experiments.insert_one({
            'id': identity, 'ownerId': owner, 'workspaceId': 'post-' + conversation,
            'createdAt': created, 'updatedAt': created, 'status': status, 'progress': {'baselineCompleted': 1},
            'reviewSnapshot': {'conversationId': conversation, 'privateLargePayload': 'x' * 1000},
            'groups': [{'large': 'x' * 1000}], 'error': '真实失败原因' if status == 'failed' else None})

    def test_all_conversations_and_statuses_are_read_without_other_owners_or_heavy_snapshots(self):
        statuses = ['completed', 'failed', 'interrupted', 'cancelled', 'draft', 'queued', 'running', 'cancelling']
        for index, status in enumerate(statuses):
            self.add(f'exp_{index}', conversation=f'c{index}', status=status)
        self.add('private', owner='another-user')
        response = handle(Request('GET', '/experiments/history', {}, {},
            {'X-CrowdSim-User': 'u', 'X-CrowdSim-Workspace': 'post-current'}, SimpleNamespace(repo=self.repo)))
        status, result, replayed = response
        self.assertEqual(200, status)
        self.assertFalse(replayed)
        self.assertEqual(8, result['total'])
        self.assertEqual(set(statuses), {item['status'] for item in result['items']})
        self.assertEqual({f'c{i}' for i in range(8)}, {item['conversationId'] for item in result['items']})
        self.assertTrue(all('groups' not in item and 'reviewSnapshot' not in item for item in result['items']))
        failed = next(item for item in result['items'] if item['status'] == 'failed')
        self.assertEqual('真实失败原因', failed['error'])
        with self.assertRaises(ApiError):
            self.repo.get('experiments', 'exp_1', 'u', 'post-current')
        self.assertEqual('failed', self.repo.get('experiments', 'exp_1', 'u', failed['workspaceId'])['status'])

    def test_cursor_pages_beyond_fifty_without_duplicates_or_gaps_when_a_new_batch_arrives(self):
        for index in range(205):
            self.add(f'exp_{index:03d}', conversation=f'c{index % 3}')
        page = self.repo.experiment_history('u', limit=100)
        found = page['items']
        self.assertEqual(205, page['total'])
        self.add('newer_batch', created='2026-11-12T11:00:00Z')
        while page['nextCursor']:
            page = self.repo.experiment_history('u', limit=100, cursor=page['nextCursor'])
            found += page['items']
        self.assertEqual(205, len(found))
        self.assertEqual({f'exp_{i:03d}' for i in range(205)}, {item['id'] for item in found})
        self.assertEqual('newer_batch', self.repo.experiment_history('u')['items'][0]['id'])

    def test_invalid_paging_is_rejected_and_workspace_listing_remains_scoped(self):
        self.add('one', conversation='c1')
        self.add('two', conversation='c2')
        self.assertEqual(['one'], [item['id'] for item in self.repo.list('experiments', 'u', 'post-c1')])
        for cursor in ('not-json', '{}', '[1,2]', '[]', json.dumps(['', 'exp'])):
            with self.subTest(cursor=cursor), self.assertRaises(ApiError):
                self.repo.experiment_history('u', cursor=cursor)
        for limit in ('0', '101', '-1', 'bad'):
            with self.subTest(limit=limit), self.assertRaises(ApiError):
                handle(Request('GET', '/experiments/history', {'limit': [limit]}, {},
                    {'X-CrowdSim-User': 'u'}, SimpleNamespace(repo=self.repo)))


if __name__ == '__main__':
    unittest.main()
