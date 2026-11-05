"""During-only model gateway. Never initializes a database or changes GLM settings."""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from data_service.config import load_database_env
from data_service.api.model_gateway import (
    ModelGateway, GatewayInputError, GatewayUpstreamError, build_payload,
    handle_gateway_error, read_json,
)

PREFIX = "/api/crowdSim/during/models"
DEEPSEEK = "deepseek-flash"
GLM = "GLM-5.3-Flash"


@dataclass(frozen=True)
class DeepSeekSettings:
    enabled: bool
    base_url: str
    model: str
    api_key: str = field(repr=False)
    proxy_url: str = field(default="", repr=False)

    @classmethod
    def load(cls):
        load_database_env()
        prefix = "CROWDSIM_DURING_DEEPSEEK_"
        return cls(
            os.environ.get(prefix + "ENABLED", "false").lower() in {"1", "true", "yes"},
            os.environ.get(prefix + "BASE_URL", "https://api.deepseek.com").rstrip("/"),
            os.environ.get(prefix + "MODEL", DEEPSEEK).strip(),
            os.environ.get(prefix + "API_KEY", "").strip(),
            os.environ.get(prefix + "PROXY", "").strip(),
        )

    @property
    def available(self):
        return self.enabled and bool(self.api_key and self.model) and self.base_url.startswith("https://")


class DuringModelGateway:
    def __init__(self, *, settings=None, glm=None, session=None):
        self.settings = settings or DeepSeekSettings.load()
        self.glm = glm or ModelGateway()
        self.session = session
        self.owns_session = session is None

    async def open(self):
        await self.glm.open()
        if self.session is None:
            self.session = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=180, connect=10))

    async def close(self):
        await self.glm.close()
        if self.owns_session and self.session is not None:
            await self.session.close()

    async def status(self):
        try:
            glm = await asyncio.wait_for(self.glm.status(), timeout=2)
        except (TimeoutError, aiohttp.ClientError):
            glm = {"id": GLM, "available": False, "message": "GLM 服务暂不可达"}
        return [glm, {"id": DEEPSEEK, "available": self.settings.available,
                      "message": "已配置，调用时验证连接" if self.settings.available else "DeepSeek 未启用或未配置后端 Key"}]

    async def chat(self, identifier, body):
        if identifier == GLM:
            return await self.glm.chat(body, GLM)
        if identifier != DEEPSEEK:
            raise GatewayInputError("请选择事中已配置的模型")
        if not self.settings.available:
            raise GatewayUpstreamError(503, "DeepSeek 未启用或未配置后端 Key")
        # Explicit, During/DeepSeek-only proxy: never inherit system proxy settings
        # or change the original GLM session. CONNECT preserves upstream TLS.
        transport = {}
        if self.settings.proxy_url:
            try:
                proxy = urlsplit(self.settings.proxy_url)
                valid = (proxy.scheme in {"http", "https"} and bool(proxy.hostname)
                         and (proxy.port is None or 1 <= proxy.port <= 65535)
                         and proxy.path in {"", "/"} and not proxy.query and not proxy.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise GatewayUpstreamError(503, "事中 DeepSeek 专用代理配置无效，请填写 HTTP 或 HTTPS 代理地址")
            transport["proxy"] = self.settings.proxy_url
        payload = build_payload(body, self.settings.model)
        payload.pop("chat_template_kwargs", None)
        payload.pop("reasoning_budget_tokens", None)
        payload["thinking"] = {"type": "disabled"}
        try:
            async with self.session.post(self.settings.base_url + "/chat/completions", json=payload,
                                         headers={"Authorization": "Bearer " + self.settings.api_key},
                                         **transport) as response:
                if response.status != 200:
                    # Upstream bodies can contain account details: return only the status.
                    raise GatewayUpstreamError(502, f"DeepSeek 返回 HTTP {response.status}")
                data = await response.json()
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
                raise GatewayUpstreamError(502, "DeepSeek 返回格式无效")
            return {**data, "model": data.get("model") or self.settings.model}
        except TimeoutError:
            raise GatewayUpstreamError(504, "DeepSeek 请求超时，请稍后重试") from None
        except (aiohttp.ClientProxyConnectionError, aiohttp.ClientHttpProxyError):
            raise GatewayUpstreamError(503, "DeepSeek 专用代理连接失败，请检查本机代理服务及 CROWDSIM_DURING_DEEPSEEK_PROXY 配置") from None
        except aiohttp.ClientSSLError:
            raise GatewayUpstreamError(503, "DeepSeek TLS 连接失败，请检查证书及 VPN、代理路由") from None
        except aiohttp.ClientConnectorError as error:
            message = ("DeepSeek DNS 解析失败，请检查 DNS 及 VPN、代理配置"
                       if isinstance(error.os_error, socket.gaierror)
                       else "DeepSeek 连接失败，请检查 VPN、代理路由及事中专用代理配置")
            raise GatewayUpstreamError(503, message) from None
        except aiohttp.ClientConnectionError:
            raise GatewayUpstreamError(503, "DeepSeek 连接被中断，请检查 VPN、代理路由及事中专用代理配置") from None
        except (aiohttp.ContentTypeError, ValueError):
            raise GatewayUpstreamError(502, "DeepSeek 返回格式无效") from None
        except aiohttp.ClientError:
            raise GatewayUpstreamError(503, "DeepSeek 请求失败，请检查公网连接和事中后端配置") from None


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
