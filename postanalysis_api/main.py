"""HTTP route adapter mounted inside the existing SUMO server on port 8765.

This module does not open a socket. ``crowdsim_overlay_server.py`` owns the
single HTTP/WebSocket listener and passes requests here.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web
from pymongo.errors import PyMongoError

from .api import datasets, experiments, model_connections, results, review_plans, simulations
from .api.common import PREFIX, Request, response
from .schemas.common import ApiError
from .services.application import PostAnalysisApp


ROUTES = (datasets.handle, simulations.handle, experiments.handle, review_plans.handle,
          results.handle, model_connections.handle)
ALLOWED_ORIGINS = {"http://localhost:8080", "http://127.0.0.1:8080"}
MAX_REQUEST_BYTES = 16 * 1024 * 1024


def _dispatch(request: Request) -> tuple[int, dict, bool]:
    if request.method == "GET" and request.path == "/capabilities":
        return response(request.app.capabilities())
    for route in ROUTES:
        result = route(request)
        if result is not None:
            return result
    raise ApiError("NOT_FOUND", "接口不存在", 404)


def _cors_headers(origin: str | None) -> dict[str, str]:
    headers = {"Access-Control-Allow-Headers": "Content-Type, Idempotency-Key, X-CrowdSim-User, X-CrowdSim-Workspace",
               "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}
    if origin in ALLOWED_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return headers


async def handle_http(http_request: web.Request, app: PostAnalysisApp) -> web.Response:
    """Serve /api/v1/post/* without occupying a second port."""
    headers = _cors_headers(http_request.headers.get("Origin"))
    if http_request.method == "OPTIONS":
        return web.Response(status=204, headers=headers)
    try:
        if http_request.remote not in {"127.0.0.1", "::1"}:
            raise ApiError("RESOURCE_FORBIDDEN", "事后 API 仅允许本机访问", 403)
        if http_request.method not in {"GET", "POST"}:
            raise ApiError("METHOD_NOT_ALLOWED", "只支持 GET、POST 和 OPTIONS", 405)
        path = http_request.path.rstrip("/")
        if path != PREFIX and not path.startswith(PREFIX + "/"):
            raise ApiError("NOT_FOUND", "接口不存在", 404)
        path = path[len(PREFIX):] or "/"
        body = {}
        if http_request.method == "POST":
            if http_request.content_length is not None and http_request.content_length > MAX_REQUEST_BYTES:
                raise ApiError("PAYLOAD_TOO_LARGE", "请求体超过 16 MiB", 413)
            raw = await http_request.content.read(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ApiError("PAYLOAD_TOO_LARGE", "请求体超过 16 MiB", 413)
            if raw:
                if http_request.content_type != "application/json":
                    raise ApiError("UNSUPPORTED_MEDIA_TYPE", "请求体必须是 application/json", 415)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ApiError("INVALID_JSON", "请求体不是有效 UTF-8 JSON", 400) from exc
                if not isinstance(body, dict):
                    raise ApiError("INVALID_JSON", "请求体必须是 JSON 对象", 400)
        query = {key: http_request.query.getall(key) for key in http_request.query}
        routed = Request(http_request.method, path, query, body, http_request.headers, app)
        status, data, replayed = await asyncio.to_thread(_dispatch, routed)
        if replayed:
            headers["Idempotency-Replayed"] = "true"
        payload = {"success": True, "data": data, "error": None}
    except ApiError as exc:
        status = exc.status
        payload = {"success": False, "data": None,
                   "error": {"code": exc.code, "message": exc.message, "field": exc.field}}
    except ValueError as exc:
        status = 400
        payload = {"success": False, "data": None,
                   "error": {"code": "INVALID_QUERY", "message": str(exc), "field": None}}
    except PyMongoError:
        logging.getLogger("postanalysis_api").exception("post analysis MongoDB operation failed")
        status = 503
        payload = {"success": False, "data": None,
                   "error": {"code": "STORAGE_UNAVAILABLE", "message": "MongoDB 暂不可用，请检查后端环境配置", "field": None}}
    except Exception:
        logging.getLogger("postanalysis_api").exception("post analysis request failed")
        status = 500
        payload = {"success": False, "data": None,
                   "error": {"code": "INTERNAL_ERROR", "message": "服务内部错误，请查看后端日志", "field": None}}
    return web.json_response(payload, status=status, headers=headers,
                             dumps=lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    raise SystemExit("事后 API 已挂载到 SUMO 的 8765 服务；请启动 crowdsim_overlay_server.py。")
