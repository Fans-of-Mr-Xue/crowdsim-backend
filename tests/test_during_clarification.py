"""Confirmed conversation contracts; no model, HTTP server, or SUMO process."""
from copy import deepcopy
import pytest
from crowdsim.experiments.contracts import ContractError, validate_experiment_config
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.domain.during_preset import load_during_preset, preset_fingerprint


def payload():
    p = load_during_preset('memorial-tower-during-v1')
    distributions = p['requirement']['population']['distributions']
    values = {
        'problemTypes': [1], 'name': '对话确认实验', 'objective': '观察聚集与行程完成',
        'location': 'memorial-tower', 'area': 'memorial-park', 'population': 700,
        'distribution': 'road-length-weighted', 'activity': 'sightseeing-600-800',
        'eventCategory': 'culture', 'extraEvent': 'none', 'control': 'none',
        'durationSeconds': 1800, 'repetitions': 2, 'metrics': ['crowding', 'completion-rate', 't95'],
        'crowdingRule': {'scope': ['H1', 'H2a', 'H2b'], 'minimumCount': 10, 'minimumFraction': .2, 'confirmationSeconds': 10},
    }
    for key, preset_key in (('crowdRole', 'crowd_role'), ('ageBand', 'age_band'), ('origin', 'origin'), ('gender', 'gender')):
        values[key] = [{k: v for k, v in e.items() if k in ('code', 'label', 'percent')} for e in distributions[preset_key]]
    binding = {'presetId': p['preset_id'], 'presetVersion': p['version'], 'presetFingerprint': preset_fingerprint(p)}
    return {
        'name': values['name'], 'researchQuestion': values['objective'], 'clientRequestId': 'confirmed-conversation',
        'methods': ['C0'], 'mode': 'formal',
        'scenario': {**binding, 'population': 700, 'durationSeconds': 1800, 'seedSet': [20260908, 20260909]},
        'clarificationSnapshot': {'schemaVersion': 1, 'stage': 'observation', 'revision': 4, **binding,
                                  'values': values, 'sources': {k: {'kind': 'user', 'messageId': 'turn-1'} for k in values}},
    }


def test_snapshot_roundtrip_and_legacy_compatibility(tmp_path):
    data = payload()
    validated = validate_experiment_config(data)
    repository = CrowdSimRepository(tmp_path)
    saved = repository.create_experiment('user', 'during-chat', validated)
    restored = CrowdSimRepository(tmp_path).get_experiment('user', 'during-chat', saved['experiment_id'])
    assert restored['clarificationSnapshot'] == data['clarificationSnapshot']
    assert restored['scenario']['duringPreset'] == load_during_preset('memorial-tower-during-v1')
    # Snapshot metadata must never rewrite the immutable runtime requirement.
    assert restored['scenario']['duringPreset']['requirement']['project']['title'] != data['name']
    del data['clarificationSnapshot']
    assert 'clarificationSnapshot' not in validate_experiment_config(data)


def test_workflow_baseline_roundtrip_preserves_execution_and_user_sources(tmp_path):
    data = payload()
    legacy = validate_experiment_config(data)
    data['clarificationSnapshot']['sources']['control'] = {'kind': 'workflow', 'messageId': ''}
    validated = validate_experiment_config(data)
    assert validated['scenario'] == legacy['scenario']
    assert validated['methods'] == ['C0']
    repo = CrowdSimRepository(tmp_path)
    saved = repo.create_experiment('u', 'w', validated)
    restored = CrowdSimRepository(tmp_path).get_experiment('u', 'w', saved['experiment_id'])
    assert restored['clarificationSnapshot'] == data['clarificationSnapshot']


@pytest.mark.parametrize(('field', 'value', 'message_id'), [
    ('population', 700, ''), ('decisionMethod', 'llm', ''),
    ('control', 'set_inflow_rate', ''), ('control', 'none', 'fabricated-user-message'),
])
def test_workflow_source_cannot_bypass_requirement_confirmation(field, value, message_id):
    data = payload()
    data['clarificationSnapshot']['values'][field] = value
    data['clarificationSnapshot']['sources'][field] = {'kind': 'workflow', 'messageId': message_id}
    with pytest.raises(ContractError):
        validate_experiment_config(data)


@pytest.mark.parametrize(('key', 'value'), [
    ('population', 1000), ('population', True), ('durationSeconds', 600), ('repetitions', 3),
    ('location', 'east-nanjing-road'), ('area', 'anywhere'), ('distribution', 'uniform'),
    ('activity', 'immediate evacuation'), ('extraEvent', 'fire'), ('eventCategory', 'emergency'),
    ('control', 'set_inflow_rate'), ('metrics', ['custom-formula']), ('metrics', ['t95', 't95']),
    ('metrics', [{}]), ('problemTypes', [2]), ('problemTypes', [True]), ('problemTypes', [1, 3]),
    ('name', 'different'), ('objective', 'different'), ('gender', [{'label': '女性', 'percent': 100}]),
    ('decisionMethod', 'xw'), ('unknownField', 'value'),
])
def test_snapshot_cannot_claim_unexecuted_requirements(key, value):
    data = payload()
    data['clarificationSnapshot']['values'][key] = value
    data['clarificationSnapshot']['sources'][key] = {'kind': 'user', 'messageId': 'test'}
    with pytest.raises(ContractError, match='clarificationSnapshot'):
        validate_experiment_config(data)


@pytest.mark.parametrize('key', ['population', 'name', 'extraEvent', 'metrics', 'crowdingRule'])
def test_missing_confirmed_requirements_are_rejected(key):
    data = payload()
    del data['clarificationSnapshot']['values'][key]
    del data['clarificationSnapshot']['sources'][key]
    with pytest.raises(ContractError):
        validate_experiment_config(data)


@pytest.mark.parametrize('mutate', [
    lambda s: s.update(presetFingerprint='outdated'),
    lambda s: s.update(presetVersion=True),
    lambda s: s.update(revision=0),
    lambda s: s.update(schemaVersion=True),
    lambda s: s.update(stage='strategy'),
    lambda s: s.update(sources={}),
    lambda s: s['sources']['population'].update(kind='unaccepted_suggestion'),
    lambda s: s['values']['crowdingRule'].update(minimumCount=9),
    lambda s: s['values']['crowdingRule'].update(minimumFraction=.3),
    lambda s: s['values']['crowdingRule'].update(confirmationSeconds=0),
    lambda s: s['values']['crowdingRule'].update(scope=['H1']),
])
def test_stale_or_unconfirmed_snapshot_is_rejected(mutate):
    data = payload()
    mutate(data['clarificationSnapshot'])
    with pytest.raises(ContractError):
        validate_experiment_config(data)


def test_multiple_goals_remain_deferred_and_do_not_enable_a_strategy_controller():
    data = payload()
    snapshot = data['clarificationSnapshot']
    snapshot['values'].update(problemTypes=[1, 2], decisionMethod='llm', phenomenonSource='先根据本轮观察结果生成策略')
    snapshot['sources'].update({k: {'kind': 'user', 'messageId': ''} for k in ('decisionMethod', 'phenomenonSource')})
    assert validate_experiment_config(data)['methods'] == ['C0']
    data['methods'] = ['C2']
    with pytest.raises(ContractError, match='only the C0 baseline'):
        validate_experiment_config(data)


def test_visible_distribution_labels_and_order_do_not_change_configuration():
    data = payload()
    for group in ('crowdRole', 'ageBand', 'origin', 'gender'):
        entries = data['clarificationSnapshot']['values'][group]
        entries.reverse()
        for entry in entries:
            entry.pop('code', None)
    assert validate_experiment_config(data)['scenario']['population'] == 700
    invalid = deepcopy(data)
    invalid['clarificationSnapshot']['values']['gender'][0]['code'] = 'wrong'
    with pytest.raises(ContractError):
        validate_experiment_config(invalid)
