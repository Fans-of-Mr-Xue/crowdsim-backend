"""Task-owned map annotations, distinct from executable rules and observations."""
from .environments import coordinate


def validate_domain_knowledge(value):
    if set(value) - {'version', 'annotations', 'note'} or value.get('version') != 1:
        raise ValueError('领域知识须使用有效的版本与空间标注格式')
    rows = value.get('annotations')
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError('空间标注最多200项')
    if not isinstance(value.get('note', ''), str) or len(value.get('note', '')) > 2000:
        raise ValueError('领域知识说明不能超过2000字')
    seen = set()
    for row in rows:
        allowed = {'id', 'kind', 'title', 'coordinates', 'status', 'description', 'source_note', 'evidence_ids', 'feature_ref'}
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError('空间标注包含不支持的字段')
        identifier = row.get('id')
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 100 or identifier in seen:
            raise ValueError('空间标注须有不重复的标识')
        seen.add(identifier)
        if row.get('kind') not in {'focus', 'population', 'control', 'facility', 'event'} or row.get('status') not in {'pending', 'assumed', 'verified'}:
            raise ValueError('请选择有效的标注类型与核对状态')
        for key, maximum in [('title', 120), ('description', 2000), ('source_note', 1000), ('feature_ref', 20000)]:
            if not isinstance(row.get(key, ''), str) or len(row.get(key, '')) > maximum:
                raise ValueError(f'空间标注的{key}格式或长度无效')
        if not row.get('title', '').strip():
            raise ValueError('请填写空间标注名称')
        coordinate(row.get('coordinates'))
        if row['status'] == 'verified' and not row.get('source_note', '').strip():
            raise ValueError('标记已核对前请填写来源与核对依据')
        evidence = row.get('evidence_ids', [])
        if not isinstance(evidence, list) or len(evidence) > 100 or any(not isinstance(item, str) or not 1 <= len(item) <= 200 for item in evidence):
            raise ValueError('关联依据须为有效的标识列表')
