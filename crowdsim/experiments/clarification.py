"""Validate the confirmed conversational snapshot without changing simulation inputs.

Legacy callers may omit the snapshot. New conversation plans preserve the exact
requirements accepted by the user, and must match the server-owned preset.
"""
from collections.abc import Mapping
import math


DISTRIBUTIONS = {'crowdRole': 'crowd_role', 'ageBand': 'age_band', 'origin': 'origin', 'gender': 'gender'}
METRICS = {'crowding', 'completion-rate', 't95', 'crowded-peak', 'red-peak', 'density-peak', 'blocked'}
REQUIRED = {'problemTypes', 'name', 'objective', 'location', 'area', 'population', *DISTRIBUTIONS,
            'distribution', 'activity', 'eventCategory', 'extraEvent', 'metrics', 'durationSeconds', 'repetitions', 'control'}
OPTIONAL = {'crowdingRule', 'decisionMethod', 'phenomenonSource'}


def _fail(message):
    raise ValueError('clarificationSnapshot: ' + message)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _distribution(entries, expected):
    if not isinstance(entries, list) or len(entries) != len(expected):
        return False
    normalized = []
    for item in entries:
        if not isinstance(item, dict) or set(item) - {'code', 'label', 'percent'}:
            return False
        if not isinstance(item.get('label'), str) or not _number(item.get('percent')):
            return False
        # Both explicit codes and the visible category labels are accepted.
        match = next((e for e in expected if item.get('code', item['label']) == e.get('code', e['label'])
                      or (not item.get('code') and item['label'] == e['label'])), None)
        if not match or item['label'] != match['label'] or item['percent'] != match['percent']:
            return False
        normalized.append(match.get('code', match['label']))
    return len(set(normalized)) == len(expected)


def validate_clarification_snapshot(data):
    if 'clarificationSnapshot' not in data:
        return
    snapshot = data['clarificationSnapshot']
    if not isinstance(snapshot, Mapping) or set(snapshot) != {'schemaVersion', 'stage', 'revision', 'values', 'sources', 'presetId', 'presetVersion', 'presetFingerprint'}:
        _fail('invalid structure')
    if type(snapshot['schemaVersion']) is not int or snapshot['schemaVersion'] != 1 or snapshot['stage'] != 'observation':
        _fail('unsupported version or stage')
    if type(snapshot['revision']) is not int or snapshot['revision'] < 1:
        _fail('revision must be a positive integer')
    scenario = data['scenario']
    preset = scenario.get('duringPreset')
    if not preset or any(snapshot[k] != scenario[k] for k in ('presetId', 'presetVersion', 'presetFingerprint')):
        _fail('preset binding mismatch')
    if type(snapshot['presetVersion']) is not int:
        _fail('invalid preset version')
    v = snapshot['values']
    sources = snapshot['sources']
    if not isinstance(v, dict) or REQUIRED - set(v) or set(v) - REQUIRED - OPTIONAL:
        _fail('requirements are incomplete or contain unknown fields')
    if not isinstance(sources, dict) or set(sources) != set(v):
        _fail('each requirement needs a source')
    for key, source in sources.items():
        if not isinstance(source, dict) or set(source) != {'kind', 'messageId'} or source['kind'] not in ('user', 'accepted_suggestion', 'workflow') or not isinstance(source['messageId'], str) or len(source['messageId']) > 160:
            _fail('invalid requirement source')
        if source['kind'] == 'workflow' and (key != 'control' or v[key] != 'none' or source['messageId'] != ''):
            _fail('only the observation baseline can be a workflow setting')
    goals = v['problemTypes']
    if not isinstance(goals, list) or any(type(n) is not int for n in goals) or goals not in ([1], [1, 2]):
        _fail('this execution must be observation; strategy generation can only be a deferred goal')
    for key, limit in (('name', 100), ('objective', 500), ('phenomenonSource', 500)):
        if key in v and (not isinstance(v[key], str) or not v[key].strip() or len(v[key]) > limit):
            _fail('invalid ' + key)
    if v['name'] != data['name'] or v['objective'] != data.get('researchQuestion'):
        _fail('title or objective differs from the experiment')
    expected = {'location': preset['location_id'], 'area': 'memorial-park', 'population': preset['requirement']['population']['total'],
                'distribution': 'road-length-weighted', 'activity': 'sightseeing-600-800',
                'eventCategory': preset['requirement']['scenario']['event_category']['id'], 'extraEvent': 'none',
                'durationSeconds': preset['experiment']['duration_seconds'], 'repetitions': len(scenario['seedSet']), 'control': 'none'}
    for key, expected_value in expected.items():
        if v[key] != expected_value or isinstance(v[key], bool):
            _fail(key + ' differs from execution')
    if type(v['population']) is not int or type(v['repetitions']) is not int or not _number(v['durationSeconds']):
        _fail('invalid population, repetitions or duration')
    for key, preset_key in DISTRIBUTIONS.items():
        if not _distribution(v[key], preset['requirement']['population']['distributions'][preset_key]):
            _fail(key + ' differs from execution')
    metrics = v['metrics']
    if not isinstance(metrics, list) or not metrics or any(not isinstance(m, str) or m not in METRICS for m in metrics) or len(set(metrics)) != len(metrics):
        _fail('unsupported metrics')
    if 'crowding' in metrics and 'crowdingRule' not in v:
        _fail('crowding rule is missing')
    if 'crowdingRule' in v:
        rule = v['crowdingRule']
        p = preset['phenomenon']
        if not isinstance(rule, dict) or set(rule) != {'scope', 'minimumCount', 'minimumFraction', 'confirmationSeconds'}:
            _fail('invalid crowding rule')
        if not isinstance(rule['scope'], list) or any(not isinstance(s, str) for s in rule['scope']) or sorted(rule['scope']) != ['H1', 'H2a', 'H2b']:
            _fail('crowding scope differs from execution')
        for key, actual in (('minimumCount', p['minimum_count']), ('minimumFraction', p['minimum_fraction']), ('confirmationSeconds', p['confirmation_seconds'])):
            if not _number(rule[key]) or rule[key] != actual:
                _fail('crowding rule differs from execution')
    if (2 in goals and v.get('decisionMethod') != 'llm') or ('decisionMethod' in v and v['decisionMethod'] != 'llm'):
        _fail('only the deferred LLM method is supported')
