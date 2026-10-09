"""Environment profile CRUD and explicit geography lookup for the frontend."""
import asyncio
import json
from aiohttp import web
from gridfs import NoFile
from pymongo.errors import PyMongoError
from .datasets import parse_json
from ..repositories.datasets import DatasetConflictError
from ..repositories.environments import EnvironmentRepository
from ..connectors.geography import GeographyConnector, GeographyError


def register_environment_routes(app, database):
    repository = EnvironmentRepository(database)
    geography = GeographyConnector(database)
    async def lifecycle(app):
        async def initialize():
            while True:
                try:
                    await asyncio.to_thread(repository.initialize)
                    await geography.initialize()
                    break
                except PyMongoError:
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
        try:
            identifier = request.match_info.get('identifier')
            action = request.match_info.get('action')
            payload = None
            if request.method in ('POST', 'PUT', 'DELETE'):
                payload = await request.json(loads=parse_json)
                if not isinstance(payload, dict):
                    raise ValueError('请求体必须为 JSON 对象')
            if action == 'search':
                data = await geography.search(request.query.get('q', ''))
            elif action == 'boundary':
                if set(payload) != {'osm_type', 'osm_id'}:
                    raise ValueError('边界查询仅接受 osm_type 和 osm_id')
                data = await geography.boundary(payload['osm_type'], payload['osm_id'])
            elif action == 'road-network':
                if set(payload) != {'geometry'}:
                    raise ValueError('路网查询仅接受 geometry')
                data = await geography.roads(payload['geometry'])
            elif action == 'density-animation':
                stream = await asyncio.to_thread(repository.open_media, identifier)
                response = web.StreamResponse(headers={'Content-Type': 'image/gif', 'Content-Length': str(stream.length), 'Cache-Control': 'public, max-age=86400', 'X-Content-Type-Options': 'nosniff'})
                try:
                    await response.prepare(request)
                    while chunk := await asyncio.to_thread(stream.read, 128 * 1024):
                        await response.write(chunk)
                    await response.write_eof()
                except ConnectionResetError:
                    pass
                finally:
                    await asyncio.to_thread(stream.close)
                return response
            elif action == 'roads':
                data = await asyncio.to_thread(repository.open_roads, identifier)
            elif request.method == 'GET':
                data = await asyncio.to_thread(repository.get, identifier) if identifier else await asyncio.to_thread(repository.list, request.query.get('search', '')[:200])
                if data is None:
                    raise KeyError(identifier)
            elif request.method == 'POST':
                data = await asyncio.to_thread(repository.save, payload)
            else:
                revision = payload.pop('revision', None)
                if type(revision) is not int or revision < 1:
                    raise ValueError('请提供有效 revision')
                if request.method == 'DELETE':
                    if payload:
                        raise ValueError('删除仅接受 revision')
                    await asyncio.to_thread(repository.delete, identifier, revision)
                    data = {'deleted': True}
                else:
                    data = await asyncio.to_thread(repository.save, payload, identifier, revision)
            return web.json_response({'data': data}, status=201 if request.method == 'POST' and not action else 200)
        except GeographyError as exc:
            return web.json_response({'message': str(exc)}, status=502)
        except DatasetConflictError as exc:
            return web.json_response({'message': str(exc)}, status=409)
        except (KeyError, NoFile):
            return web.json_response({'message': '区域画像或附件不存在'}, status=404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except PyMongoError:
            return web.json_response({'message': '数据库暂不可用，请检查数据服务和数据库连接'}, status=503)

    for prefix in ('/api/crowdSim', '/crowdSim'):
        root = prefix + '/environment-profiles'
        app.router.add_get(root + '/geography/{action:search}', handle)
        app.router.add_post(root + '/geography/{action:boundary|road-network}', handle)
        app.router.add_get(root, handle)
        app.router.add_post(root, handle)
        app.router.add_get(root + '/{identifier}/media/{action:density-animation}', handle)
        app.router.add_get(root + '/{identifier}/{action:roads}', handle)
        app.router.add_get(root + '/{identifier}', handle)
        app.router.add_put(root + '/{identifier}', handle)
        app.router.add_delete(root + '/{identifier}', handle)
    return repository
