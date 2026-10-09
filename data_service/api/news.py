"""HTTP only enqueues jobs; it never runs crawlers in the data/SUMO process."""
import asyncio
import json
from aiohttp import web
from pymongo.errors import PyMongoError
from ..repositories.news import NewsRepository
from .datasets import parse_json
from ..connectors.llm import JsonModelClient, ModelError
from ..services.news_keywords import recommend_keywords
from .models import selected_request_model


def register_news_routes(app, database, *, model=None):
    repository = NewsRepository(database)
    own_model = model is None
    model = model if model is not None else JsonModelClient()
    recommendation_slots = asyncio.Semaphore(2)

    async def lifecycle(app):
        if own_model:
            await model.open()
        try:
            yield
        finally:
            if own_model:
                await model.close()
    app.cleanup_ctx.append(lifecycle)

    async def recommendations(request):
        try:
            payload = await request.json(loads=parse_json)
            provider = selected_request_model(request)
            request_model = model.for_provider(provider) if provider and hasattr(model, 'for_provider') else model
            async with asyncio.timeout(60):
                async with recommendation_slots:
                    data = await recommend_keywords(payload, repository.environments, request_model)
            return web.json_response({'data': data})
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return web.json_response({'message': str(error)[:300]}, status=400)
        except (ModelError, TimeoutError) as error:
            return web.json_response({'message': str(error)[:300] or '大模型推荐超时，请重试或自行新增标签'}, status=502)
        except PyMongoError:
            return web.json_response({'message': '环境库暂不可用，请检查数据库连接'}, status=503)

    async def handle(request):
        try:
            identifier = request.match_info.get("identifier")
            action = request.match_info.get("action")
            status = 200
            if request.method == "DELETE":
                data = await asyncio.to_thread(repository.delete, identifier)
            elif request.method == "POST":
                if action == "cancel":
                    data = await asyncio.to_thread(repository.cancel, identifier)
                else:
                    payload = await request.json(loads=parse_json)
                    data = await asyncio.to_thread(repository.create, payload)
                    status = 201 if data["operation"] == "created" else 200
            elif action == "news":
                page, size = pagination(request, 100)
                data = await asyncio.to_thread(repository.news, identifier, page, size)
            elif identifier:
                data = await asyncio.to_thread(repository.get, identifier)
                if data is None:
                    raise KeyError(identifier)
            else:
                page, size = pagination(request, 100)
                data = await asyncio.to_thread(repository.list, page, size)
            return web.json_response({"data": data}, status=status)
        except KeyError:
            return web.json_response({"message": "采集任务不存在"}, status=404)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return web.json_response({"message": str(error)[:300] or "请求参数无效"}, status=400)
        except PyMongoError:
            return web.json_response({"message": "数据库暂不可用，请检查数据服务与 SSH 隧道"}, status=503)

    for prefix in ("/api/crowdSim", "/crowdSim"):
        app.router.add_post(prefix + '/news-collection/recommend-keywords', recommendations)
        path = prefix + "/news-collection/tasks"
        app.router.add_get(path, handle)
        app.router.add_post(path, handle)
        app.router.add_get(path + "/{identifier}", handle)
        app.router.add_delete(path + "/{identifier}", handle)
        app.router.add_get(path + "/{identifier}/{action:news}", handle)
        app.router.add_post(path + "/{identifier}/{action:cancel}", handle)
    return repository


def pagination(request, maximum):
    page, size = int(request.query.get("page", 1)), int(request.query.get("page_size", 50))
    if not (1 <= page <= 100000 and 1 <= size <= maximum):
        raise ValueError("分页参数超出范围")
    return page, size
