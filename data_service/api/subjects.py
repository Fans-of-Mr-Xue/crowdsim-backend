"""Subject library CRUD, isolated from SUMO and model execution."""
import asyncio
import json
import logging

from aiohttp import web
from pymongo.errors import PyMongoError

from .datasets import parse_json
from ..repositories.datasets import DatasetConflictError
from ..repositories.subjects import SubjectRepository


def register_subject_routes(app, database):
    repository = SubjectRepository(database)
    ready = asyncio.Event()

    async def lifecycle(app):
        async def initialize():
            while True:
                try:
                    await asyncio.to_thread(repository.initialize)
                    ready.set()
                    return
                except PyMongoError:
                    logging.getLogger(__name__).warning('主体库初始化等待数据库连接')
                    await asyncio.sleep(3)
        task = asyncio.create_task(initialize())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    app.cleanup_ctx.append(lifecycle)

    async def handle(request):
        if not ready.is_set():
            return web.json_response({'message': '主体库正在连接数据库，请稍后刷新'}, status=503)
        try:
            identifier = request.match_info.get('identifier')
            if request.method == 'GET':
                data = await asyncio.to_thread(repository.get, identifier) if identifier else await asyncio.to_thread(repository.list, request.query.get('search', '')[:200])
                if data is None:
                    raise KeyError(identifier)
            else:
                payload = await request.json(loads=parse_json)
                if not isinstance(payload, dict):
                    raise ValueError('请求体必须为 JSON 对象')
                revision = payload.pop('revision', None)
                if request.method in ('PUT', 'DELETE') and (type(revision) is not int or revision < 1):
                    raise ValueError('请提供有效 revision')
                if request.method == 'DELETE':
                    if payload:
                        raise ValueError('删除仅接受 revision')
                    await asyncio.to_thread(repository.delete, identifier, revision)
                    data = {'deleted': True}
                else:
                    data = await asyncio.to_thread(repository.save, payload, identifier, revision)
            return web.json_response({'data': data}, status=201 if request.method == 'POST' else 200)
        except DatasetConflictError as exc:
            return web.json_response({'message': str(exc)}, status=409)
        except KeyError:
            return web.json_response({'message': '主体不存在或已删除'}, status=404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except PyMongoError:
            return web.json_response({'message': '数据库暂不可用，请检查数据服务和数据库连接'}, status=503)

    for prefix in ('/api/crowdSim', '/crowdSim'):
        root = prefix + '/subject-profiles'
        app.router.add_get(root, handle)
        app.router.add_post(root, handle)
        app.router.add_get(root + '/{identifier}', handle)
        app.router.add_put(root + '/{identifier}', handle)
        app.router.add_delete(root + '/{identifier}', handle)
    return repository
