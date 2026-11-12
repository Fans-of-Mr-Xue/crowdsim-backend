"""Pure repository/handler checks. Never start HTTP, MongoDB, a model or SUMO."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dialog_storage.during_requirements import DuringRequirementRepository, RequirementConflict
from dialog_storage.during_workflow import DuringWorkflowRepository, WorkspaceConflict, experiment_group_id
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.scenarios.class2_society import publish_class2_society, load_class2_society


def confirmed(kind=1):
    requirement = {'schema_version': 2, 'research': {'problem_type': kind}, 'project': {'title': '研究'},
                   'population': {'total': 20, 'distributions': {}}, 'experiment': {'duration_seconds': 100},
                   'observation': {'metric_ids': ['global-speed']},
                   'class2': {'flow_design': {'version': 1, 'network_sha256': 'network', 'flows': []}}}
    snapshot = {'id': 'snapshot', 'draftId': 'draft', 'revision': 1, 'confirmedAt': '2026-11-11T00:00:00Z',
                'requirement': deepcopy(requirement), 'modeling_requirements': {'revision': 1, 'individual': {}, 'environment': {}, 'rules': {}}}
    return {'requirement': {'version': 3, 'draft': {'id': 'draft', 'revision': 1, 'requirement': requirement,
            'confirmation': {'id': 'snapshot'}, 'savedSnapshotId': 'snapshot', 'assistantTurns': {}},
            'snapshots': [snapshot], 'submissions': {}, 'modelingRequirements': snapshot['modeling_requirements']},
            'observation': None, 'legacy': None}


class Artifacts:
    def load(self, cid, sid):
        return {'conversationId': cid, 'artifacts': {}, 'selectedSnapshotId': sid}


@pytest.fixture
def storage(tmp_path):
    requirements = DuringRequirementRepository(tmp_path / 'requirements')
    workflows = DuringWorkflowRepository(tmp_path / 'workflows', requirements, Artifacts())
    with patch.dict('os.environ', {'CROWDSIM_EXPERIMENT_DIR': str(tmp_path / 'control')}):
        yield requirements, workflows


@pytest.mark.parametrize('kind', [1, 2])
def test_confirmation_is_unique_immutable_and_survives_restart(storage, kind):
    requirements, workflows = storage
    state = confirmed(kind)
    accepted = requirements.save('study', state=state, expected_revision=0, mutation_id='confirm')
    assert requirements.save('study', state=state, expected_revision=0, mutation_id='confirm') == accepted
    reopened = DuringRequirementRepository(requirements.root)
    assert reopened.load('study')['workflow']['experimentId'] == experiment_group_id('study')
    assert workflows.load('study')['snapshotId'] == 'snapshot'
    assert workflows.load('other') is None
    for change in ('requirement', 'type', 'modeling', 'second-snapshot', 'reset'):
        edited = deepcopy(state)
        if change == 'requirement':
            edited['requirement']['draft']['requirement']['project']['title'] = '变更'
        elif change == 'type':
            edited['requirement']['draft']['requirement']['research']['problem_type'] = 3
        elif change == 'modeling':
            edited['requirement']['modelingRequirements'] = {'revision': 99}
        elif change == 'second-snapshot':
            edited['requirement']['snapshots'].append({**edited['requirement']['snapshots'][0], 'id': 'other'})
        else:
            edited['requirement']['snapshots'] = []
        with pytest.raises(ValueError):
            requirements.save('study', state=edited, expected_revision=1, mutation_id=change)
    state['requirement']['draft']['assistantTurns']['message'] = {'reply': '继续分析'}
    requirements.save('study', state=state, expected_revision=1, mutation_id='chat')
    assert requirements.load('study')['revision'] == 2


def test_unconfirmed_draft_is_never_published(storage):
    requirements, _ = storage
    state = confirmed()
    state['requirement']['snapshots'] = []
    with pytest.raises(ValueError):
        requirements.save('draft-only', state=state, expected_revision=0, mutation_id='draft')
    assert requirements.load('draft-only') is None


def test_workspace_products_are_cas_scoped_and_durable(storage):
    requirements, workflows = storage
    for cid in ('a', 'b'):
        requirements.save(cid, state=confirmed(), expected_revision=0, mutation_id='confirm')
    version = workflows.load('a')['workspace']['evidenceVersion']
    body = {'snapshotId': 'snapshot', 'key': 'crowdsim_during_evaluation_results', 'value': '{"score":80}',
            'expectedRevision': 0, 'mutationId': 'evaluation', 'evidenceVersion': version}
    accepted = workflows.save('a', body)
    assert workflows.save('a', body) == accepted
    reopened = DuringWorkflowRepository(workflows.root, requirements, Artifacts())
    assert reopened.load('a')['workspace']['values'][body['key']] == body['value']
    assert reopened.load('b')['workspace']['values'] == {}
    with pytest.raises(WorkspaceConflict):
        workflows.save('a', {**body, 'mutationId': 'stale', 'value': 'new'})
    with pytest.raises(ValueError):
        workflows.save('a', {**body, 'snapshotId': 'foreign'})
    with pytest.raises(ValueError):
        workflows.save('a', {**body, 'key': 'editor.unsaved'})


def test_reset_and_round_restart_invalidate_saved_evaluations(storage):
    requirements, workflows = storage
    requirements.save('study', state=confirmed(), expected_revision=0, mutation_id='confirm')
    controls = CrowdSimRepository(data_dir=Path(__import__('os').environ['CROWDSIM_EXPERIMENT_DIR']))
    binding = {'conversationId': 'study', 'snapshotId': 'snapshot'}
    group = controls.create_experiment('crowdsim-local-user', 'during-study', {'duringObservation': binding, 'clientRequestId': 'one'})
    run = controls.create_run('crowdsim-local-user', 'during-study', group['experiment_id'], 'C0', 1, {})
    version = workflows.load('study')['workspace']['evidenceVersion']
    body = {'snapshotId': 'snapshot', 'key': 'crowdsim_during_evaluation_results', 'value': '{}',
            'expectedRevision': 0, 'mutationId': 'evaluation', 'evidenceVersion': version}
    workflows.save('study', body)
    controls.update_run('crowdsim-local-user', 'during-study', run['run_id'], {'restartCount': 1})
    assert body['key'] not in workflows.load('study')['workspace']['values']
    with pytest.raises(WorkspaceConflict):
        workflows.save('study', {**body, 'mutationId': 'late-old-run'})


def test_one_group_per_conversation_with_new_request_tokens(tmp_path):
    repo = CrowdSimRepository(tmp_path)
    config = {'duringObservation': {'conversationId': 'a', 'snapshotId': 'snapshot'}, 'clientRequestId': 'one'}
    first = repo.create_experiment('user', 'during-a', config)
    assert repo.create_experiment('user', 'during-a', {**config, 'clientRequestId': 'lost-response'})['experiment_id'] == first['experiment_id']
    with pytest.raises(ValueError):
        repo.create_experiment('user', 'during-a', {**config, 'duringObservation': {'conversationId': 'a', 'snapshotId': 'other'}})
    generic = repo.create_experiment('user', 'pre', {'clientRequestId': 'generic'})
    run = repo.create_run('user', 'pre', generic['experiment_id'], 'C0', 1, {})
    repo.update_run('user', 'pre', run['run_id'], {'status': 'running'})
    repo.recover_during_observations()
    assert repo.get_run('user', 'pre', run['run_id'])['status'] == 'running'


def test_second_class_publishes_matching_model_without_starting_an_engine(storage, tmp_path):
    requirements, _ = storage
    state = confirmed(2)
    requirements.save('study', state=state, expected_revision=0, mutation_id='confirm')
    r = state['requirement']['draft']['requirement']
    receipt = {'requirement_id': 'req-' + 'a' * 32, 'fingerprint': 'sha256:test', 'requirement': deepcopy(r),
               'class2_demo': {'source': {'conversationId': 'study', 'snapshotId': 'snapshot'}, 'count': 20,
                              'duration_seconds': 100, 'flow_design': deepcopy(r['class2']['flow_design']), 'layout_sha256': 'layout'}}
    first = publish_class2_society(receipt, requirements=requirements, root=tmp_path)
    assert publish_class2_society(receipt, requirements=requirements, root=tmp_path) == first
    assert first['status'] == 'published'
    assert load_class2_society(receipt['requirement_id'], receipt['class2_demo']['source'], root=tmp_path) == first
    assert first['artificial_society']['modelingRequirements'] == state['requirement']['snapshots'][0]['modeling_requirements']
    with pytest.raises(ValueError):
        publish_class2_society({**receipt, 'class2_demo': {**receipt['class2_demo'], 'count': 21}}, requirements=requirements, root=tmp_path)
    path = tmp_path / 'artificial_societies/class2' / (first['model_id'] + '.json')
    edited = json.loads(path.read_text()); edited['artificial_society']['population']['total'] = 99
    path.write_text(json.dumps(edited))
    with pytest.raises(ValueError, match='指纹'):
        load_class2_society(receipt['requirement_id'], receipt['class2_demo']['source'], root=tmp_path)


def test_workflow_handlers_do_not_require_an_http_listener(storage):
    from types import SimpleNamespace
    from dialogue_service.server import DialogueHandler
    requirements, workflows = storage
    requirements.save('study', state=confirmed(), expected_revision=0, mutation_id='confirm')
    handler = DialogueHandler.__new__(DialogueHandler)
    handler.path = '/during/conversations/study/workflow'
    handler.server = SimpleNamespace(during_workflows=workflows)
    handler._permitted = lambda: True
    replies = []
    handler._send = lambda status, body: replies.append((status, body))
    handler.do_GET()
    assert replies[-1][0] == 200 and replies[-1][1]['record']['snapshotId'] == 'snapshot'
    body = {'snapshotId': 'snapshot', 'key': 'crowdsim_during_evaluation_results', 'value': '{}',
            'expectedRevision': 0, 'mutationId': 'saved-result',
            'evidenceVersion': replies[-1][1]['record']['workspace']['evidenceVersion']}
    handler._json_body = lambda: body
    handler.do_POST(); assert replies[-1][0] == 200
    body = {**body, 'mutationId': 'stale'}
    handler.do_POST(); assert replies[-1][0] == 409


def test_artifact_publication_and_workflow_loading_share_no_inverse_lock_order(storage, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from dialog_storage.during_artifacts import DuringArtifactRepository
    requirements, _ = storage
    requirements.save('study', state=confirmed(), expected_revision=0, mutation_id='confirm')
    artifacts = DuringArtifactRepository(tmp_path / 'artifacts', requirements, runs_root=tmp_path / 'runs')
    workflows = DuringWorkflowRepository(tmp_path / 'workflows', requirements, artifacts)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(10):
            jobs = [pool.submit(workflows.load, 'study'), pool.submit(artifacts.save, 'study', {'action': 'select_plan', 'snapshotId': 'snapshot'})]
            for job in jobs:
                job.result(timeout=2)
