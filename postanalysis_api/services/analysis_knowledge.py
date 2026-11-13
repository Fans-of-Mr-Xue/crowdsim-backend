"""Read user-selected existing libraries as traceable analysis context."""
from postanalysis_api.schemas.common import ApiError

LIBRARIES = {'cases': ('case_profiles', '风险案例库'),
             'plans': ('emergency_plans', '应急预案库'),
             'research': ('research_documents', '研究文献库'),
             'regulations': ('regulations', '法规库')}
PHASE_FIELDS = {'observation': ('summary', 'topic', 'trigger_conditions'),
                'intervention': ('core_actions', 'mandatory_requirements', 'summary'),
                'mechanism': ('key_provisions', 'summary', 'trigger_conditions')}


def read_context(repo, experiment_id, user, workspace, libraries):
    task = repo.get('experiments', experiment_id, user, workspace)
    if task['status'] != 'completed' or not task.get('resultId'):
        raise ApiError('RESULT_NOT_READY', '请选择已完成并封存结果的反事实实验。', 409)
    if len(libraries) != len(set(libraries)) or any(key not in LIBRARIES for key in libraries):
        raise ApiError('INVALID_QUERY', '知识库选择无效', 400)
    sources, counts = [], {}
    fields = {'name': 1, 'summary': 1, 'topic': 1, 'trigger_conditions': 1, 'core_actions': 1,
              'mandatory_requirements': 1, 'key_provisions': 1, 'sourceUrl': 1, 'source_url': 1}
    for key in libraries:
        collection, label = LIBRARIES[key]
        query = {'is_deleted': {'$ne': True}}
        if key != 'cases':
            query['parse_status'] = 'parsed'
        counts[key] = repo.db[collection].count_documents(query)
        for doc in repo.db[collection].find(query, fields).sort([('updated_at', -1), ('_id', 1)]):
            notes = {phase: '；'.join(str(doc[field])[:800] for field in names if doc.get(field))
                     for phase, names in PHASE_FIELDS.items()}
            sources.append({'id': str(doc['_id']), 'library': key, 'libraryName': label,
                            'name': doc.get('name', ''), 'url': doc.get('sourceUrl') or doc.get('source_url', ''),
                            'notes': notes})
    return {'experimentId': experiment_id, 'datasetVersion': task['datasetVersion'],
            'libraries': libraries, 'counts': counts, 'sources': sources}
