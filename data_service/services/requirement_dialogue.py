"""Ground an open-ended consultation in available methods and prior evidence."""
from ..prediction.catalog import method_catalog


def requirement_dialogue(context, history, task):
    fields = {item['id']: item.get('value') for item in context['fields']}
    state = (context.get('state') or {}).get('task-definition') or {}
    current_id = (context.get('task_reference') or {}).get('id')
    previous = []
    # Preserve titles/IDs and borrowing evidence across fresh search rankings.
    # A retrieved candidate is not necessarily one the assistant recommended.
    for message in reversed(history):
        if message.get('page') != 'task-definition' or message.get('role') != 'assistant':
            continue
        reference_id = (message.get('task_reference') or {}).get('id')
        if reference_id and reference_id != current_id:
            continue
        retrieval = (message.get('plan') or {}).get('retrieval') or {}
        previous.append({
            'reply': message.get('content', '')[:3000],
            'case_candidates': [{key: row.get(key) for key in
                                 ('id', 'title', 'revision', 'adoption_note', 'borrowing_evidence', 'differences')}
                                for row in retrieval.get('items', [])],
            'document_candidates': [{key: row.get(key) for key in
                                     ('id', 'title', 'revision', 'adoption_note', 'limitations', 'url')}
                                    for row in retrieval.get('documents', [])],
        })
        if len(previous) >= 4:
            break
    from ..prediction.consultation_catalog import consultation_catalog
    return {
        'input_and_experiment_contract': consultation_catalog(),
        'previous_discussions_newest_first': previous,
        'reference_resolution': '按用户提到的名称或此前回复的推荐顺序解析；检索候选顺序不等于推荐顺序。不能唯一定位时先追问。候选未自动获采纳。',
        'adopted_cases': state.get('references') or [],
        'adopted_case_ids': fields.get('task.references') or [],
        'available_numerical_methods': [
            {**row, 'path': 'professional', 'availability': '已实现计算；本轮未运行'}
            for row in method_catalog(task)
        ],
        'method_scope': '上述目录是当前事前三模型比较的执行目录；旧版方法不再默认推荐。',
        'other_paths': [],
        'review_policy': '先主动提出有理由的建议，通过对话中的confirmations提供同意/不同意按钮。案例采纳须用户点击对应按钮，或由用户手动在参考依据中采纳；不因一个“可以”同时确认案例、方法和全部参数。',
    }
