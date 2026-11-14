"""Resolve explicitly linked notices for the activity and scope under study."""
from datetime import datetime
from urllib.parse import urlparse


def validate_notice(row):
    notice = row.get('activity_notice')
    if notice is None:
        return
    if not isinstance(notice, dict) or set(notice) != {'activity_id', 'scope', 'claim', 'authority'}:
        raise ValueError('活动公告需要关联活动、适用范围、消息性质和来源类型')
    for field in ('id', 'title', 'source', 'content', 'date'):
        if not isinstance(row.get(field), str) or not row[field].strip() or len(row[field]) > 2000:
            raise ValueError('请填写公告标题、来源、发布时间和内容')
    if not isinstance(notice['activity_id'], str) or not isinstance(notice['scope'], dict):
        raise ValueError('公告所属活动或研究范围无效')
    if notice['claim'] not in ('confirmed', 'cancelled') or notice['authority'] not in ('organizer', 'report'):
        raise ValueError('公告须标明举办或取消，以及主办方或其他报道')
    published = datetime.fromisoformat(row['date'])
    if published.tzinfo or 'T' not in row['date']:
        raise ValueError('请使用上海本地日期和时间记录公告发布时间')
    if row.get('url'):
        url = urlparse(row['url'])
        if url.scheme not in ('https', 'http') or not url.netloc:
            raise ValueError('公告链接须为 HTTP 或 HTTPS 地址')


def resolve_activity(activity, evidence, scope):
    notices = []
    for row in evidence:
        link = row.get('activity_notice') or {}
        if link.get('activity_id') != activity['id'] or link.get('scope') != scope:
            continue
        validate_notice(row)
        notices.append({**{k: row.get(k, '') for k in ('id', 'title', 'source', 'content', 'date', 'url')},
                        'claim': link['claim'], 'authority': link['authority']})
    notices.sort(key=lambda row: (datetime.fromisoformat(row['date']), row['id']), reverse=True)
    official = [row for row in notices if row['authority'] == 'organizer']
    decisive = []
    status = 'pending'
    if official:
        latest = datetime.fromisoformat(official[0]['date'])
        decisive = [row for row in official if datetime.fromisoformat(row['date']) == latest]
        claims = {row['claim'] for row in decisive}
        status = claims.pop() if len(claims) == 1 else 'conflict'
    elif len({row['claim'] for row in notices}) > 1:
        status, decisive = 'conflict', notices
    # Older saved activities were explicit user scenarios, including the monument demo.
    assumed = activity.get('assume_if_pending', activity['enabled'])
    included = activity['enabled'] and (status == 'confirmed' or (status == 'pending' and assumed))
    potential = activity['attendance'] * activity['visit_share'] * activity['additional_share']
    return {'activity_id': activity['id'], 'title': activity['title'], 'status': status,
            'included': included, 'potential_visits': potential, 'visits': potential if included else 0,
            'start': activity['start'], 'end': activity['end'], 'assumption': activity['note'],
            'decisive_ids': [row['id'] for row in decisive], 'notices': notices}
