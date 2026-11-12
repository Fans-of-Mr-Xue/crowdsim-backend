"""Restore a frozen batch from successfully archived group/seed runs."""
from copy import deepcopy

from postanalysis_api.schemas.common import ApiError


class SupersededExecution(InterruptedError):
    """An older worker must not write into a newer execution attempt."""


def run_matrix(task):
    groups = task.get('groups', [])
    count = max((group['repeatCount'] for group in groups), default=1)
    seed = task.get('seedStart', task.get('seed', 0))
    return ([('F-00', 'baseline', seed + index, []) for index in range(count)] +
            [(group['id'], group['experimentType'], seed + index, group['interventions'])
             for group in groups for index in range(group['repeatCount'])])


def completed_batch_runs(repository, task):
    expected = {(plan, seed) for plan, _kind, seed, _actions in run_matrix(task)}
    found = {}
    for run in repository.completed_runs_for_experiment(task['id'], task['ownerId'], task['workspaceId']):
        key = (run.get('planId'), run.get('seed'))
        if key not in expected:
            continue
        if (run.get('experimentId') != task['id'] or run.get('datasetVersion') != task['datasetVersion']
                or run.get('scenario') is not None and run['scenario'] != task['scenario']):
            raise ApiError('RUN_VERSION_MISMATCH', '已完成运行与原批次数据或场景版本不一致，未覆盖原记录。', 409)
        if key in found:
            raise ApiError('DUPLICATE_COMPLETED_RUN', '同一实验组和种子有多份已完成记录，请先核对，未自动选取或覆盖。', 409)
        found[key] = run
    return found


def restored_progress(task, completed):
    progress = deepcopy(task['progress'])
    progress['failed'] = 0
    if 'groups' in task:
        progress['baselineCompleted'] = sum(plan == 'F-00' for plan, _seed in completed)
        progress['interventionCompleted'] = sum(plan != 'F-00' for plan, _seed in completed)
        progress['groups'] = [{**row, 'completed': sum(plan == row['groupId'] for plan, _seed in completed), 'failed': 0}
                              for row in progress['groups']]
    else:
        progress['completed'] = len(completed)
    return progress
