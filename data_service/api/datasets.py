"""Dataset HTTP API; can run independently of SUMO."""
from __future__ import annotations

import asyncio
import json
from aiohttp import web
from pymongo.errors import PyMongoError

from ..repositories.datasets import DatasetConflictError, DatasetRepository


def reject_nonfinite(value):
    raise ValueError("不支持非有限数值")


def parse_json(raw):
    return json.loads(raw, parse_constant=reject_nonfinite)


def register_database_routes(app, repository=None):
    repository = repository if repository is not None else DatasetRepository()

    async def cleanup(app):
        repository.close()
    app.on_cleanup.append(cleanup)

    async def handle(request):
        def response(data, status=200):
            return web.json_response({"data": data}, status=status)
        try:
            action = request.match_info.get("action")
            if action == "status":
                return response(await asyncio.to_thread(repository.status))
            if action == "smoke-test":
                return response(await asyncio.to_thread(repository.smoke_test))
            identifier = request.match_info.get("identifier")
            if request.method == "GET":
                if identifier:
                    data = await asyncio.to_thread(repository.get, identifier)
                    if data is None:
                        raise KeyError(identifier)
                else:
                    page = int(request.query.get("page", 1))
                    size = int(request.query.get("page_size", 20))
                    if not (1 <= page <= 100000 and 1 <= size <= 100):
                        raise ValueError("page 或 page_size 超出范围")
                    data = await asyncio.to_thread(repository.list, page=page, page_size=size,
                                                   search=request.query.get("search", "")[:200],
                                                   source=request.query.get("source", "")[:200])
                return response(data)
            try:
                payload = await request.json(loads=parse_json)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ValueError("请求体必须是有效 JSON")
            if request.method == "POST":
                return response(await asyncio.to_thread(repository.create, payload), 201)
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            revision = payload.pop("revision", None)
            if type(revision) is not int or revision < 1:
                raise ValueError("更新必须提供有效 revision")
            return response(await asyncio.to_thread(repository.update, identifier, payload, revision))
        except DatasetConflictError as exc:
            return web.json_response({"message": str(exc)}, status=409)
        except KeyError:
            return web.json_response({"message": "数据集不存在"}, status=404)
        except (ValueError, TypeError, RecursionError) as exc:
            return web.json_response({"message": str(exc)}, status=400)
        except PyMongoError:
            # Driver errors may contain connection details; never return them to browsers.
            return web.json_response({"message": "数据库暂不可用，请检查后端配置和 SSH 隧道"}, status=503)

    for prefix in ("/api/crowdSim", "/crowdSim"):
        app.router.add_get(prefix + "/database/{action:status}", handle)
        app.router.add_post(prefix + "/database/{action:smoke-test}", handle)
        app.router.add_get(prefix + "/datasets", handle)
        app.router.add_post(prefix + "/datasets", handle)
        app.router.add_get(prefix + "/datasets/{identifier}", handle)
        app.router.add_put(prefix + "/datasets/{identifier}", handle)
    return repository

