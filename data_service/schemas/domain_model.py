"""Review state belongs to the exact saved scenario, never just a task ID."""
import hashlib
import json


def model_fingerprint(task):
    components = task.get('components', {})
    value = {key: task.get(key) for key in ('title', 'summary', 'event_id', 'event_label', 'time_window', 'region', 'population', 'references')}
    value['components'] = {key: value for key, value in components.items() if key != 'domain_model_review'}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def reconcile_review(task):
    review = task.get('components', {}).get('domain_model_review')
    if review and review.get('status') == 'confirmed' and review.get('confirmed_fingerprint') != model_fingerprint(task):
        review['status'] = 'pending_confirmation'
        review.pop('confirmed_at', None)
        review.pop('confirmed_fingerprint', None)
