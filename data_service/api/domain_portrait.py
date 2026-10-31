"""Read and edit the evidence-backed domain model without running simulations."""
import asyncio
import json

from aiohttp import web
from pymongo.errors import PyMongoError

from .datasets import parse_json
from ..repositories.datasets import DatasetConflictError
from ..services.domain_portrait import DomainPortraitService, build_portrait
from ..connectors.amap_traffic import AmapTrafficConnector
from ..connectors.llm import JsonModelClient, ModelError
from .models import selected_request_model
from ..services.domain_retrieval import retrieve


def register_domain_portrait_routes(app, database, *, task_roads):
    service = DomainPortraitService(database)
    traffic = AmapTrafficConnector()
    model = JsonModelClient()
    slots = asyncio.Semaphore(1)
    async def lifecycle(app):
        await model.open()
        try:
            yield
        finally:
            await model.close()
    app.cleanup_ctx.append(lifecycle)

    async def search(request):
        if slots.locked():
            return web.json_response({'message': '已有资料检索进行中，请稍后重试'}, status=429)
        try:
            async with slots, asyncio.timeout(240):
                task = await asyncio.to_thread(service.read, request.match_info['identifier'])
                provider = selected_request_model(request)
                task, roads, metadata = await context(task)
                portrait = await asyncio.to_thread(build_portrait, task, roads, metadata)
                news = await asyncio.to_thread(service.activity_news, task)
                data = await retrieve(task, model.for_provider(provider) if provider else model, portrait.get('evidence', []), news)
                await asyncio.to_thread(database.domain_retrieval.update_one,
                    {'task_id': task['id'], 'revision': task['revision']}, {'$set': data}, upsert=True)
                return web.json_response({'data': data})
        except KeyError:
            return web.json_response({'message': '任务不存在'}, status=404)
        except (ValueError, ModelError, TimeoutError) as exc:
            return web.json_response({'message': str(exc)[:240] or '资料检索超时，请重试'}, status=502)
        except PyMongoError:
            return web.json_response({'message': '检索记录暂时无法保存'}, status=503)


    async def traffic_status(request):
        try:
            task = await asyncio.to_thread(service.read, request.match_info['identifier'])
            result = await traffic.read(task, int(request.query.get('radius', '1000')))
            return web.json_response({'data': result}, headers={'Cache-Control': 'no-store'})
        except KeyError:
            return web.json_response({'message': '分析任务不存在'}, status=404)
        except (ValueError, TypeError):
            return web.json_response({'message': '路况查询范围无效'}, status=400)
        except PyMongoError:
            return web.json_response({'message': '暂时无法读取研究范围'}, status=503)

    async def context(task):
        task = await task_roads.task_context(task)
        metadata = task['road_network']
        roads = await asyncio.to_thread(task_roads.snapshots.read, metadata['id']) if metadata.get('id') else None
        return task, roads, metadata

    async def handle(request):
        try:
            identifier = request.match_info['identifier']
            task, roads, metadata = await context(await asyncio.to_thread(service.read, identifier))
            if request.method == 'PUT':
                payload = await request.json(loads=parse_json)
                if not isinstance(payload, dict) or set(payload) - {'revision', 'section', 'data', 'time_window'} or not {'revision', 'section', 'data'} <= set(payload) or type(payload['revision']) is not int or payload['revision'] < 1:
                    raise ValueError('请提供有效的任务版本、修改栏目与数据')
                if 'time_window' in payload and payload['section'] != 'settings':
                    raise ValueError('研究时间请在通用场景参数中修改')
                task = await asyncio.to_thread(service.save, identifier, payload, roads, metadata)
                task, roads, metadata = await context(task)
            news = await asyncio.to_thread(service.activity_news, task)
            portrait = await asyncio.to_thread(build_portrait, task, roads, metadata, news)
            portrait['retrieval'] = await asyncio.to_thread(database.domain_retrieval.find_one, {'task_id': task['id'], 'revision': task['revision']}, {'_id': 0})
            return web.json_response({'data': {'task': task, 'portrait': portrait, 'roads': roads, 'road_network': metadata}}, headers={'Cache-Control': 'no-store'})
        except KeyError:
            return web.json_response({'message': '分析任务或路网资料不存在'}, status=404)
        except DatasetConflictError as exc:
            return web.json_response({'message': str(exc)}, status=409)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except PyMongoError:
            return web.json_response({'message': '数据库暂不可用，请稍后重新读取领域模型'}, status=503)

    for prefix in ('/api/crowdSim', '/crowdSim'):
        path = prefix + '/analysis-tasks/{identifier}/domain-portrait'
        app.router.add_get(path, handle)
        app.router.add_put(path, handle)
        app.router.add_get(path + '/traffic', traffic_status)
        app.router.add_post(path + '/search', search)
