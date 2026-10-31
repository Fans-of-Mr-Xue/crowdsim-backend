"""Explainable lexical retrieval over actual MongoDB case records."""
import re
from ..repositories.cases import CaseRepository
from .case_recommendations import recommendation_content


class RequirementCaseSearch:
    def __init__(self, database):
        self.cases = CaseRepository(database)
        self.documents = database['research_documents']

    def search(self, query, task=None, limit=6):
        task = task or {}
        seed = ' '.join([query, task.get('title', ''), task.get('event_label', ''), task.get('region', {}).get('name', '')])
        # Dates, hours and isolated counts do not express case relevance.
        terms = {term for term in re.findall(r'[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}', seed.lower())
                 if not term.isdigit()}
        terms |= {word for word in ('上海', '外滩', '纪念塔', '跨年', '节庆', '国庆', '散场', '人群', '拥堵', '踩踏', '广场', '公园', '游客', '暴雨', '内涝', '洪水') if word in seed}
        # Bigrams also match a place or phrase embedded in a longer sentence.
        terms |= {word[i:i + 2] for word in list(terms) if re.fullmatch(r'[\u4e00-\u9fff]+', word) for i in range(len(word) - 1)}
        terms -= {'帮我', '一下', '分析', '研究', '需要', '这个', '情况', '当前', '区域', '进行', '预测', '任务', '研判', '哪些', '时间', '日期', '小时', '分钟', '设置', '确定', '修改', '希望', '提供', '案例', '相似', '相关', '资料', '参考', '内容'}
        ranked = []
        for doc in self.cases.collection.find({'is_deleted': {'$ne': True}}, {'detail_pages': 0}):
            fields = [('名称', doc.get('name', ''), 4), ('事件类型', ' '.join([doc.get('topic', ''), doc.get('type', '')]), 3),
                      ('地点', ' '.join([doc.get('city', ''), doc.get('locationName', ''), doc.get('locationType', '')]), 3),
                      ('摘要', doc.get('summary', ''), 1)]
            hits = [(label, sorted((t for t in terms if t in value.lower()), key=len, reverse=True), weight) for label, value, weight in fields]
            score = sum(weight * sum(min(len(term), 8) for term in found) for _, found, weight in hits)
            content = recommendation_content(doc, pre_event=True)
            if not score or content is None:
                continue
            reasons = [f'{label}涉及“{"、".join(found[:3])}”' for label, found, _ in hits if found]
            ranked.append({'id': doc['_id'], 'revision': doc.get('revision', 1), 'title': doc.get('name', ''),
                'category': doc.get('topic', ''), 'location': doc.get('locationName') or doc.get('city', ''),
                'date': doc.get('eventDate', ''), 'summary': doc.get('summary', '')[:1800],
                'sources': (doc.get('sources') or doc.get('sourceLinks') or ([{'title': doc.get('sourceLabel') or '案例来源', 'url': doc['sourceUrl']}] if doc.get('sourceUrl') else []))[:6],
                **content, 'reason': '与本次关注内容的关联：' + '；'.join(reasons), 'score': score,
                'differences': '名称或主题相关不等于条件相同；活动规模、地形、年代及措施的适用性需核对。'})
        ranked.sort(key=lambda item: (-item['score'], item['id']))
        documents = []
        # Search parsed metadata only; this is not a claim of full-text retrieval.
        projection = {key: 1 for key in ('name', 'summary', 'methods', 'application_value', 'limitations', 'revision', 'source_url')}
        for doc in self.documents.find({'is_deleted': {'$ne': True}, 'parse_status': 'parsed'}, projection):
            text = ' '.join(str(doc.get(key) or '') for key in ('name', 'summary', 'methods', 'application_value')).lower()
            hits = sorted((term for term in terms if term in text), key=len, reverse=True)
            if not hits:
                continue
            documents.append({'id': str(doc['_id']), 'title': doc.get('name', ''), 'revision': doc.get('revision', 1),
                              'summary': doc.get('summary', '')[:1000],
                              'adoption_note': (doc.get('application_value') or doc.get('methods') or '待核对原文后确定具体借鉴内容')[:800],
                              'limitations': doc.get('limitations', '')[:600] or '需核对研究对象、数据年代与本场景是否适用。',
                              'url': '/api/crowdSim/research-documents/' + str(doc['_id']) + '/file',
                              'score': sum(min(len(term), 8) for term in hits)})
        documents.sort(key=lambda item: (-item['score'], item['id']))
        return {'items': ranked[:limit], 'total_matches': len(ranked), 'query': query,
                'documents': documents[:limit], 'document_matches': len(documents),
                'strategy': '按事件、地点和风险关键词检索数据库案例，忽略纯数字；借鉴建议来自案例记录，采纳前请确认适用性。'}
