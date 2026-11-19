from copy import deepcopy
from data_service.prediction.demo import demo_inputs
import unittest

from data_service.services.forecast_demand import resolve_forecast_inputs
from data_service.services.domain_demand import initialize, project
from data_service.services.domain_portrait import build_portrait


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.task, _, settings = demo_inputs()
        self.task['time_window'] = '2026-12-31 23:45 至 2027-01-01 00:15'
        self.task['components']['forecast_setup'] = dict(settings, initial_crowd=900)
        history = dict(title='历史样本', scope='测试区域', evidence_id='history',
                       records=[{'date':'2025-12-31', 'daily_visits':4000}])
        self.config = initialize(self.task, {'visualizations':{'history':history}})
        self.config['assumptions'].update(local_share=.5, window_share=2/3)
        self.activity = dict(id='countdown', source_id='scenario', source_label='情景设定',
                             source_url='', title='倒计时', start='2026-12-31T23:55',
                             end='2027-01-01T00:05', attendance=600, visit_share=1,
                             additional_share=1, note='测试假设', enabled=True)
        self.config['activities'] = [self.activity]
        self.portrait = {'demand_inputs': self.config}
        self.task['components']['domain_portrait'] = self.portrait
        self.evidence = self.portrait.setdefault('evidence', [])

    def notice(self, claim='cancelled', source='organizer', date='2026-11-12T09:00'):
        row = dict(id='source-' + str(len(self.evidence)), title='模拟公告', source='演示来源', content='核对本次活动',
                   date=date, activity_notice=dict(activity_id=self.activity['id'], scope=deepcopy(self.config['scope']),
                                                   claim=claim, authority=source))
        self.evidence.append(row)
        return row

    def inputs(self):
        settings, source = resolve_forecast_inputs(self.task, {'arrival_rate_per_minute': 99}, strict=True)
        self.assertEqual(settings['initial_crowd'], 900)
        self.assertEqual(settings['arrival_schedule'], project(self.task, self.portrait)['forecast_inputs']['arrival_schedule'])
        return source['total_visits'], [(r['minute'], r['rate']) for r in settings['arrival_schedule']], source

    def test_cancel_restore_and_timing(self):
        self.assertEqual(self.inputs()[:2], (1800, [(0,40), (10,100), (20,40)]))
        notice = self.notice()
        total, curve, snapshot = self.inputs()
        self.assertEqual((total, curve), (1200, [(0,40)]))
        self.assertEqual(snapshot['activity_decisions'][0]['decisive_ids'], [notice['id']])
        self.evidence.clear()
        self.activity['timing'] = dict(event_at='2027-01-01T00:00', lead_minutes=10, lag_minutes=0)
        self.assertEqual(self.inputs()[:2], (1800, [(0,40), (5,100), (15,40)]))
        self.assertEqual(snapshot['activity_decisions'][0]['visits'], 0)

    def test_conflict_and_later_authoritative_notice(self):
        self.notice('confirmed')
        self.notice()
        self.assertEqual(self.inputs()[2]['activity_decisions'][0]['status'], 'conflict')
        self.assertEqual(self.inputs()[0], 1200)
        self.notice('confirmed', date='2026-11-12T10:00')
        self.notice('cancelled', source='report', date='2026-11-12T11:00')
        self.assertEqual(self.inputs()[0], 1800)

    def test_notice_support_tracks_scope_conflict_and_supersession(self):
        def score(row):
            return build_portrait(self.task)['evidence_strengths']['source:' + row['id']]['score']

        notice = self.notice('confirmed')
        self.assertEqual(score(notice), 2)
        notice['url'] = 'https://example.org/notice'
        self.assertEqual(score(notice), 3)
        self.notice('cancelled')
        self.assertIsNone(score(notice))
        latest = self.notice('cancelled', date='2026-11-12T11:00')
        self.assertEqual(score(notice), 0)
        latest['evidence_origin'] = 'scenario'
        self.assertEqual(score(latest), 0)
        latest['activity_notice']['scope']['geometry_hash'] = 'another-site'
        notice['activity_notice']['scope']['geometry_hash'] = 'another-site'
        self.assertIsNone(score(notice))

    def test_scope_and_explicit_scenario(self):
        self.activity['assume_if_pending'] = False
        self.notice('confirmed', 'report')
        self.notice('confirmed')['activity_notice']['scope']['geometry_hash'] = 'other'
        self.assertEqual(self.inputs()[0], 1200)
        self.activity['assume_if_pending'] = True
        self.assertEqual(self.inputs()[0], 1800)
