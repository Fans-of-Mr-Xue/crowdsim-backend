"""During-only model gateway. Never initializes a database or changes GLM settings."""
from __future__ import annotations

import argparse
import asyncio

import aiohttp
from aiohttp import web

from data_service.api.model_gateway import (
    ModelGateway, GatewayInputError, handle_gateway_error, read_json,
)

PREFIX = "/api/crowdSim/during/models"
GLM = "GLM-5.3-Flash"


class DuringModelGateway:
    def __init__(self, *, glm=None):
        self.glm = glm or ModelGateway()

    async def open(self):
        await self.glm.open()

    async def close(self):
        await self.glm.close()

    async def status(self):
        try:
            glm = await asyncio.wait_for(self.glm.status(), timeout=2)
        except (TimeoutError, aiohttp.ClientError):
            glm = {"id": GLM, "available": False, "message": "GLM 服务暂不可达"}
        return [glm]

    async def chat(self, identifier, body):
        if identifier != GLM:
            raise GatewayInputError("请选择事中已配置的 GLM 模型")
        return await self.glm.chat(body, GLM)


def register_during_model_routes(app, *, gateway=None):
    gateway = gateway or DuringModelGateway()

    async def lifecycle(_):
        await gateway.open()
        try:
            yield
        finally:
            await gateway.close()

    async def health(_):
        return web.json_response({"service": "crowdsim-during-models", "databaseRequired": False})

    async def status(_):
        return web.json_response({"data": await gateway.status()}, headers={"Cache-Control": "no-store"})

    async def chat(request):
        async def operation():
            return web.json_response(await gateway.chat(request.match_info["model"], await read_json(request)))
        return await handle_gateway_error(operation)

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/api/crowdSim/during/health", health)
    app.router.add_get(PREFIX + "/status", status)
    app.router.add_post(PREFIX + "/{model}/chat/completions", chat)
    return gateway


def create_app():
    app = web.Application(client_max_size=8 * 1024 * 1024)
    register_during_model_routes(app)
    return app


def main():
    parser = argparse.ArgumentParser(description="事中独立模型网关（无数据库、无 SSH）")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    web.run_app(create_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
