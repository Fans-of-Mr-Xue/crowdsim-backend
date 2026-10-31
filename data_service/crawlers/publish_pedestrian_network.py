"""Publish a prepared public-data edition and explicitly update related task bindings."""
import argparse
import asyncio
import json
from pathlib import Path

from ..repositories.datasets import DatasetRepository
from ..repositories.environments import EnvironmentRepository
from ..repositories.analysis_tasks import AnalysisTaskRepository
from ..services.task_roads import TaskRoadService
from ..schemas.environments import FIELDS, validate_roads


async def publish(environment_id, asset, update_tasks, correct_legacy_channels=False):
    database = DatasetRepository()
    try:
        environments = EnvironmentRepository(database.db)
        environment = await asyncio.to_thread(environments.get, environment_id)
        if not environment:
            raise ValueError('环境库区域不存在')
        network = validate_roads(json.loads(Path(asset).read_text(encoding='utf-8')))
        if network.get('source', {}).get('provider') == 'sumo' and network.get('boundary') != environment['geometry']:
            raise ValueError('环境库边界已变化，请重新从 SUMO 导出后发布')
        existing = await asyncio.to_thread(environments.open_roads, environment_id)
        # Stable content apart from quality generation times makes repeated
        # deployment safe, without silently creating another edition each start.
        def content(value):
            value = json.loads(json.dumps(value))
            value.get('quality', {}).pop('generated_at', None)
            return value
        if content(existing) != content(network):
            payload = {key: value for key, value in environment.items() if key in FIELDS and key != 'polygon'}
            if network.get('source', {}).get('provider') == 'sumo':
                message = f"SUMO 共用路网：{len(network['features'])}条通道、{len(network.get('walkable_areas', {}).get('features', []))}处路口步行区域；宽度沿用仿真参数"
            else:
                message = f"公开资料细化：{network['quality']['surface_feature_count']}条地面通道、{len(network['features']) - network['quality']['surface_feature_count']}条其他层通道；宽度缺项未补造"
            payload.update(roads=network, roadsStatus='available', roadsMessage=message)
            environment = await asyncio.to_thread(environments.save, payload, environment_id, environment['revision'])
        print(json.dumps({'environment_id': environment_id, 'environment_revision': environment['revision'], 'quality': network.get('quality')}, ensure_ascii=False), flush=True)
        if not update_tasks:
            return
        tasks, service = AnalysisTaskRepository(database.db), TaskRoadService(database.db, environments)
        candidates = list(database.db.analysis_tasks.find({'is_deleted': {'$ne': True}}, {'_id': 1}))
        for item in candidates:
            task = await asyncio.to_thread(tasks.get, item['_id'])
            binding = task.get('components', {}).get('road_network_binding') or {}
            snapshot = await asyncio.to_thread(service.snapshots.latest, task)
            sources = snapshot.get('source', {}).get('environments', []) if snapshot else []
            # Upgrade only tasks that were already using this environment.
            linked = task['region'].get('environment_id') == environment_id or binding.get('environment_id') == environment_id or any(source['environment_id'] == environment_id for source in sources)
            if not linked:
                continue
            new_binding = {'environment_id': environment_id, 'environment_revision': environment['revision']}
            setup = task.get('components', {}).get('forecast_setup') or {}
            old_points = {'A': [121.485785, 31.244686], 'B': [121.485998, 31.243742], 'C': [121.48636, 31.242516]}
            corrections = network.get('access_audit', {}).get('channels') or []
            # Correct only the known screenshot approximation, never replace a
            # subsequently edited task or reinterpret map POIs as a gate census.
            correct = correct_legacy_channels and corrections and setup.get('exit_points') == old_points and '按用户截图' in setup.get('exit_points_note', '')
            if binding != new_binding or correct:
                fields = ('title', 'summary', 'event_id', 'event_label', 'time_window', 'template_case', 'status', 'region', 'population', 'references', 'components')
                payload = {key: task[key] for key in fields}
                payload['components']['road_network_binding'] = new_binding
                if correct:
                    payload['components']['forecast_setup'] = {**setup,
                        'exit_points': {item['id']: item['coordinates'] for item in corrections},
                        'exit_labels': {item['id']: item['name'] for item in corrections},
                        'exit_points_note': network['access_audit']['summary'] + ' 开放时间、宽度与来流比例仍为情景假设；未核验 2014 年状态。'}
                task = await asyncio.to_thread(tasks.save, payload, task['id'], task['revision'])
            _, snapshot = await service.resolve(task, refresh=True)
            print(json.dumps({'task_id': task['id'], 'task_revision': task['revision'], 'road_snapshot': snapshot['id'], 'feature_count': snapshot['feature_count']}, ensure_ascii=False), flush=True)
    finally:
        database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment-id', required=True)
    parser.add_argument('--asset', required=True)
    parser.add_argument('--update-linked-tasks', action='store_true')
    parser.add_argument('--correct-legacy-channels', action='store_true', help='仅修正未被用户改动的截图近似通道点位')
    args = parser.parse_args()
    asyncio.run(publish(args.environment_id, args.asset, args.update_linked_tasks, args.correct_legacy_channels))


if __name__ == '__main__':
    main()
