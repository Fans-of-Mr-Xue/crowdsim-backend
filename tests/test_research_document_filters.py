"""Document type filtering must precede pagination and combine with other filters."""
import unittest
from unittest.mock import patch

import mongomock
from mongomock.gridfs import enable_gridfs_integration

from data_service.repositories.research_documents import ResearchDocumentRepository


class ResearchDocumentFilterTests(unittest.TestCase):
    def setUp(self):
        enable_gridfs_integration()
        self.database = mongomock.MongoClient().crowdsim
        self.repository = ResearchDocumentRepository(self.database)
        records = [dict(_id=f'paper-{index:02}', name=f'城市论文 {index}', type='研究论文', topic='城市内涝') for index in range(21)]
        records += [dict(_id='law-1', name='城市防洪条例', type='法律法规', topic='防汛应急'),
                    dict(_id='law-2', name='城市安全条例', type='法律法规', topic='人群动力学'),
                    dict(_id='report', name='城市研究报告', summary='报告引用了研究论文', type='研究报告', topic='城市内涝'),
                    dict(_id='deleted', name='已删除文献', type='已删除类型', topic='其他', is_deleted=True)]
        for record in records:
            self.repository.collection.insert_one({'is_deleted': False, 'parse_status': 'parsed', 'updated_at': '2026-10-09', **record})
        aggregate = self.repository.collection.aggregate

        def compatible_aggregate(pipeline):
            # mongomock lacks MongoDB's $unset stage; exclusion projection is equivalent.
            pipeline = [{'$project': {stage['$unset']: 0}} if '$unset' in stage else stage for stage in pipeline]
            return aggregate(pipeline)
        self.compatibility = patch.object(self.repository.collection, 'aggregate', side_effect=compatible_aggregate)
        self.compatibility.start()
        self.addCleanup(self.compatibility.stop)

    def test_type_applies_before_pagination_and_does_not_match_mentions(self):
        first = self.repository.list(page_size=20, document_type='研究论文')
        second = self.repository.list(page=2, page_size=20, document_type='研究论文')
        self.assertEqual(first['total'], 21)
        self.assertEqual(len(first['items']), 20)
        self.assertEqual(len(second['items']), 1)
        self.assertTrue(all(item['type'] == '研究论文' for item in first['items'] + second['items']))

    def test_combined_filters_and_full_type_catalog(self):
        result = self.repository.list(search='城市', topic='防汛应急', document_type='法律法规')
        self.assertEqual([item['id'] for item in result['items']], ['law-1'])
        self.assertEqual(result['total'], 1)
        self.assertEqual(set(result['types']), {'法律法规', '研究论文', '研究报告'})
        self.assertEqual(result['stats']['total'], 24)
        self.assertEqual(self.repository.list(document_type='不存在')['total'], 0)
