import unittest
from data_service.services.domain_retrieval import grounded_finding

class EvidenceGroundingTests(unittest.TestCase):
    def test_requires_verbatim_evidence(self):
        text = '官方公告：公园开放时间为每日六点至二十二点。'
        value = dict(relevant=True, quote='公园开放时间为每日六点至二十二点。', date='未明确', scope='公园', use='开放时段')
        self.assertEqual(grounded_finding(value, text)['quote'], value['quote'])
        value['quote'] = '纪念塔跨年期间预计到访人数为两万人。'
        self.assertIsNone(grounded_finding(value, text))

    def test_rejects_irrelevant_and_short_snippets(self):
        self.assertIsNone(grounded_finding(dict(relevant=False, quote='一段不相关的网页事实记录内容'), '一段不相关的网页事实记录内容'))
        self.assertIsNone(grounded_finding(dict(relevant=True, quote='未知'), '未知'))

class OfficialDiscoveryTests(unittest.TestCase):
    def test_discovery_stays_on_official_host_and_deduplicates(self):
        from data_service.services.domain_retrieval import discover_links, official_index
        base = 'https://www.shanghai.gov.cn/nw12344/index.html'
        html = '<a href="/nw12344/20261029/abc.html">外滩活动</a><a href="/nw12344/20261029/abc.html">外滩活动</a><a href="https://evil.example/20261029/a.html">外滩活动</a><a href="/nw12344/20261029/b.html">无关内容</a>'
        rows = discover_links(html, base, ['外滩'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(official_index(rows[0]['url']), base)
        self.assertIsNone(official_index('https://example.com/nw12344/abc'))
