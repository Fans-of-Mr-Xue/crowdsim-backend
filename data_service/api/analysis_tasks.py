"""Analysis task CRUD; all persistence runs outside the simulation process."""
import asyncio
import json
from contextlib import suppress
from aiohttp import web
from pymongo.errors import PyMongoError
from .datasets import parse_json
from ..repositories.analysis_tasks import AnalysisTaskRepository
from ..repositories.datasets import DatasetConflictError
from ..services.task_roads import TaskRoadService
from ..connectors.geography import GeographyError


def register_analysis_task_routes(app, database, *, task_roads=None):
    repository = AnalysisTaskRepository(database)
    roads = task_roads or TaskRoadService(database, repository.environments)

    async def lifecycle(app):
        async def initialize():
            while True:
                try:
                    await asyncio.to_thread(repository.initialize)
                    await asyncio.to_thread(roads.snapshots.initialize)
                    return
                except PyMongoError:
                    await asyncio.sleep(3)
        task = asyncio.create_task(initialize())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    app.cleanup_ctx.append(lifecycle)

    async def handle(request):
        try:
            identifier = request.match_info.get('identifier')
            component = request.match_info.get('component')
            status = 200
            if request.method == 'GET':
                if identifier:
                    data = await asyncio.to_thread(repository.get, identifier)
                    if data is None:
                        raise KeyError(identifier)
                    if not component:
                        data = await roads.task_context(data)
                    else:
                        if component not in data['components']:
                            raise KeyError(component)
                        data = {'task_id': identifier, 'revision': data['revision'], 'name': component, 'data': data['components'][component]}
                else:
                    page, size = int(request.query.get('page', 1)), int(request.query.get('page_size', 20))
                    if not 1 <= page <= 100000 or not 1 <= size <= 100:
                        raise ValueError('分页参数超出范围')
                    data = await asyncio.to_thread(repository.list, page, size, request.query.get('search', '')[:200])
            else:
                payload = await request.json(loads=parse_json)
                if not isinstance(payload, dict):
                    raise ValueError('请求体必须为 JSON 对象')
                revision = payload.pop('revision', None)
                if identifier and (type(revision) is not int or revision < 1):
                    raise ValueError('请提供有效 revision')
                if component:
                    if set(payload) != {'data'}:
                        raise ValueError('组件更新仅接受 data 和 revision')
                    data = await asyncio.to_thread(repository.put_component, identifier, component, payload['data'], revision)
                elif request.method == 'DELETE':
                    if payload:
                        raise ValueError('删除仅接受 revision')
                    await asyncio.to_thread(repository.delete, identifier, revision)
                    data = {'deleted': True}
                else:
                    data = await asyncio.to_thread(repository.save, payload, identifier, revision)
                    status = 200 if identifier else 201
                if request.method != 'DELETE':
                    data = await roads.task_context(data)
            return web.json_response({'data': data}, status=status, headers={'Cache-Control': 'no-store'})
        except KeyError:
            return web.json_response({'message': '分析任务或组件不存在'}, status=404)
        except DatasetConflictError as exc:
            return web.json_response({'message': str(exc)}, status=409)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except PyMongoError:
            return web.json_response({'message': '数据库暂不可用，任务未确认保存，请检查后端连接'}, status=503)

    async def road_network(request):
        try:
            task = await asyncio.to_thread(repository.get, request.match_info['identifier'])
            if task is None:
                raise KeyError('task')
            if request.method == 'PUT':
                payload = await request.json(loads=parse_json)
                if not isinstance(payload, dict) or set(payload) - {'revision', 'roads', 'filename', 'environment_id'} or not {'revision', 'roads', 'filename'} <= set(payload) or type(payload['revision']) is not int:
                    raise ValueError('导入路网须提供 revision、roads 和 filename')
                if payload['revision'] != task['revision']:
                    raise DatasetConflictError('任务版本已变化，请重新读取任务')
                filename = payload['filename']
                if not isinstance(filename, str) or not 1 <= len(filename) <= 200:
                    raise ValueError('路网文件名须为 1–200 字文本')
                from ..services.environment_networks import publish_network
                current = await roads.describe(task)
                environment_id = payload.get('environment_id') or current.get('source', {}).get('primary_environment_id') or task['region'].get('environment_id')
                if not isinstance(environment_id, str) or not environment_id:
                    raise ValueError('请选择需要管理该路网的环境库区域')
                environment = await asyncio.to_thread(publish_network, repository.environments, environment_id, payload['roads'], filename, task_boundary=task['region']['geometry'])
                fields = ('title', 'summary', 'event_id', 'event_label', 'time_window', 'template_case', 'status', 'region', 'population', 'references', 'components')
                updated = {key: task[key] for key in fields}
                updated['components']['road_network_binding'] = {'environment_id': environment['id'], 'environment_revision': environment['revision']}
                try:
                    task = await asyncio.to_thread(repository.save, updated, task['id'], task['revision'])
                except DatasetConflictError:
                    raise DatasetConflictError('路网已保存至环境库，但任务同时发生变化；请刷新任务后重新关联环境库版本') from None
                geometry, snapshot = await roads.resolve(task, refresh=True)
                metadata = {'status': 'saved', **snapshot}
            elif request.method == 'POST':
                payload = await request.json(loads=parse_json)
                if not isinstance(payload, dict) or set(payload) != {'revision'} or type(payload['revision']) is not int:
                    raise ValueError('更新路网只接受任务 revision')
                if payload['revision'] != task['revision']:
                    raise DatasetConflictError('任务版本已变化，请重新读取任务')
                geometry, snapshot = await roads.resolve(task, refresh=True)
                metadata = {'status': 'saved', **snapshot}
            else:
                snapshot_id = request.query.get('snapshot_id')
                if snapshot_id:
                    snapshot = await asyncio.to_thread(roads.snapshots.get, snapshot_id)
                    if snapshot is None or snapshot['task_id'] != task['id']:
                        raise KeyError('snapshot')
                    metadata = {'status': 'saved', **snapshot}
                else:
                    metadata = await roads.describe(task)
                geometry = await asyncio.to_thread(roads.snapshots.read, metadata['id']) if metadata.get('id') else {'type': 'FeatureCollection', 'features': []}
            return web.json_response({'data': {'task_id': task['id'], 'task_revision': task['revision'], 'road_network': metadata, 'roads': geometry,
                                            **({'task': task} if request.method == 'PUT' else {})}}, headers={'Cache-Control': 'no-store'})
        except KeyError:
            return web.json_response({'message': '任务或路网快照不存在'}, status=404)
        except DatasetConflictError as exc:
            return web.json_response({'message': str(exc)}, status=409)
        except GeographyError as exc:
            return web.json_response({'message': str(exc)}, status=502)
        except (ValueError, TypeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except PyMongoError:
            return web.json_response({'message': '数据库暂不可用，无法读取任务路网'}, status=503)

    for prefix in ('/api/crowdSim', '/crowdSim'):
        root = prefix + '/analysis-tasks'
        app.router.add_get(root, handle)
        app.router.add_post(root, handle)
        app.router.add_get(root + '/{identifier}', handle)
        app.router.add_put(root + '/{identifier}', handle)
        app.router.add_delete(root + '/{identifier}', handle)
        app.router.add_get(root + '/{identifier}/road-network', road_network)
        app.router.add_post(root + '/{identifier}/road-network', road_network)
        app.router.add_put(root + '/{identifier}/road-network', road_network)
        app.router.add_get(root + '/{identifier}/components/{component}', handle)
        app.router.add_put(root + '/{identifier}/components/{component}', handle)
    return repository
