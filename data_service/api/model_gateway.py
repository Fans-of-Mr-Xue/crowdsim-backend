"""OpenAI-compatible CrowdSim model gateway hosted by the data service."""
from __future__ import annotations

import json
import os
import asyncio
from typing import Any

import aiohttp
from aiohttp import web

from ..config import load_database_env
from ..connectors.models import model_configs, selected_provider

PUBLIC_MODEL_ID = "GLM-5.3-Flash"
UPSTREAM_MODEL_ID = "GLM-5.3-Flash"
MODEL_ALIASES = frozenset({PUBLIC_MODEL_ID, "GLM-5.3-Flash-8800"})
DEFAULT_BASE_URL = "http://127.0.0.1:8800/v1"
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_TOKENS_LIMIT = 32768
ALLOWED_EXTRA_FIELDS = frozenset({
    "temperature", "top_p", "response_format", "tools", "tool_choice",
    "chat_template_kwargs", "reasoning_budget_tokens",
})


class GatewayInputError(ValueError):
    pass


class GatewayUpstreamError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def gateway_settings() -> tuple[str, str]:
    load_database_env()
    base_url = os.environ.get("CROWDSIM_MODEL_GATEWAY_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    if base_url != DEFAULT_BASE_URL:
        raise ValueError("CROWDSIM_MODEL_GATEWAY_BASE_URL 当前只允许本机 8800 /v1")
    return base_url, os.environ.get("CROWDSIM_MODEL_API_KEY", "")


def resolve_model(model: Any) -> str:
    value = str(model or "").strip()
    if value in MODEL_ALIASES:
        return PUBLIC_MODEL_ID
    try:
        selected_provider(value, model_configs())
    except ValueError:
        raise GatewayInputError("模型不可用，请选择已配置的模型")
    return value


def validate_messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 200:
        raise GatewayInputError("messages 必须是 1～200 项的数组")
    messages = []
    for item in value:
        if not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant", "tool"}:
            raise GatewayInputError("messages 包含无效角色或格式")
        content = item.get("content")
        if not isinstance(content, (str, list)):
            raise GatewayInputError("messages.content 必须是字符串或内容数组")
        messages.append(dict(item))
    return messages


def build_payload(body: dict[str, Any], model: str) -> dict[str, Any]:
    unknown = set(body) - ({"messages", "stream", "max_tokens"} | ALLOWED_EXTRA_FIELDS)
    if unknown:
        raise GatewayInputError("请求包含不支持的字段：" + ", ".join(sorted(unknown)))
    if body.get("stream") not in (None, False):
        raise GatewayInputError("CrowdSim 模型网关当前不支持流式响应")
    max_tokens = body.get("max_tokens", 2048)
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS_LIMIT:
        raise GatewayInputError(f"max_tokens 必须是 1～{MAX_TOKENS_LIMIT} 的整数")
    payload = {"model": model, "messages": validate_messages(body.get("messages")),
               "stream": False, "max_tokens": max_tokens}
    for key in ALLOWED_EXTRA_FIELDS:
        if key in body:
            payload[key] = body[key]
    return payload


def normalize_chat_response(value: Any, model=PUBLIC_MODEL_ID) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("choices"), list):
        raise GatewayUpstreamError(502, "模型服务返回格式无效")
    result = dict(value)
    result["model"] = model
    return result


def response_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def read_json(request: web.Request) -> dict[str, Any]:
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise GatewayInputError("请求体超过 8 MB")
    try:
        body = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise GatewayInputError("请求体必须是有效 JSON") from None
    if not isinstance(body, dict):
        raise GatewayInputError("请求体必须是 JSON 对象")
    return body


class ModelGateway:
    def __init__(self, *, session: aiohttp.ClientSession | None = None,
                 base_url: str | None = None, api_key: str | None = None) -> None:
        configured_url, configured_key = gateway_settings()
        self.base_url = (base_url or configured_url).rstrip("/")
        self.api_key = configured_key if api_key is None else api_key
        self.session = session
        self.owns_session = session is None
        self.providers = model_configs()
        self.providers[PUBLIC_MODEL_ID].update(base_url=self.base_url, api_key=self.api_key)

    async def open(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=180, connect=5, sock_read=175),
                trust_env=False,
            )

    async def close(self) -> None:
        if self.owns_session and self.session is not None:
            await self.session.close()
            self.session = None

    async def status(self) -> dict[str, Any]:
        try:
            async with self.session.get(self.base_url + "/models",
                                        headers=response_headers(self.api_key)) as response:
                if response.status != 200:
                    return {"id": PUBLIC_MODEL_ID, "available": False,
                            "message": f"模型服务返回 HTTP {response.status}", "port": 8800}
                payload = await response.json()
            ids = {str(item.get("id") or "") for item in payload.get("data", []) if isinstance(item, dict)}
            available = UPSTREAM_MODEL_ID in ids
            return {"id": PUBLIC_MODEL_ID, "available": available,
                    "message": "可用" if available else "8800 未加载 GLM-5.3-Flash", "port": 8800}
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
            return {"id": PUBLIC_MODEL_ID, "available": False,
                    "message": "模型服务不可达，请检查 8800 隧道", "port": 8800}

    async def chat(self, body: dict[str, Any], public_model: str) -> dict[str, Any]:
        identifier = resolve_model(public_model)
        provider, discovered = selected_provider(identifier, self.providers)
        payload = build_payload(body, provider['model'])
        if discovered:
            try:
                async with self.session.get(provider['base_url'] + '/models',
                        headers=response_headers(provider['api_key']), timeout=aiohttp.ClientTimeout(total=4)) as response:
                    if response.status != 200:
                        raise GatewayUpstreamError(503, '无法核对所选模型的可用性')
                    advertised = await response.json()
                if provider['model'] not in {item.get('id') for item in advertised.get('data', []) if isinstance(item, dict)}:
                    raise GatewayInputError('所选模型已不可用，请重新展开模型菜单')
            except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as exc:
                if isinstance(exc, GatewayInputError):
                    raise
                raise GatewayUpstreamError(503, '无法核对所选模型的可用性') from None
        try:
            async with self.session.post(provider['base_url'] + "/chat/completions", json=payload,
                                         headers=response_headers(provider['api_key'])) as response:
                raw = await response.text()
                if response.status != 200:
                    raise GatewayUpstreamError(502, f"模型服务返回 HTTP {response.status}")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                raise GatewayUpstreamError(502, "模型服务返回格式无效") from None
            return normalize_chat_response(value, identifier)
        except GatewayUpstreamError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise GatewayUpstreamError(503, f"模型服务不可达或请求超时，请检查 {provider['port']} 端口映射") from None

    async def statuses(self):
        async def probe(identifier):
            provider = self.providers[identifier]
            try:
                async with self.session.get(provider['base_url'] + '/models',
                        headers=response_headers(provider['api_key']),
                        timeout=aiohttp.ClientTimeout(total=4)) as response:
                    if response.status != 200:
                        raise ValueError('模型接口不可用')
                    payload = await response.json()
                names = {str(item.get('id', '')).casefold() for item in payload.get('data', []) if isinstance(item, dict)}
                available = provider['model'].casefold() in names
                message = '可用' if available else '接口可达，但未加载配置的模型'
                configured_names = {item['model'].casefold() for item in self.providers.values()
                                    if item['base_url'] == provider['base_url']}
                extras = [{**{key: provider[key] for key in ('port',)}, 'id': identifier + '::' + item['id'],
                           'label': item['id'], 'model': item['id'], 'available': True, 'message': '可用'}
                          for item in payload.get('data', []) if isinstance(item, dict) and isinstance(item.get('id'), str)
                          and item['id'] and item['id'].casefold() not in configured_names]
            except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
                available, message = False, '模型服务不可达，请检查服务和端口映射'
                extras = []
            return [{**{key: provider[key] for key in ('label', 'port', 'model')},
                    'id': identifier, 'available': available, 'message': message}, *extras]
        groups = await asyncio.gather(*(probe(identifier) for identifier in
            (PUBLIC_MODEL_ID, 'Qwen3.8-Flash-Next', 'DiffusionGemma')))
        rows, seen = [], set()
        for group in groups:
            for row in group:
                endpoint = self.providers[row['id'].split('::', 1)[0]]['base_url']
                key = (endpoint, row['model'].casefold())
                if key not in seen:
                    rows.append(row)
                    seen.add(key)
        return rows


async def handle_gateway_error(operation):
    try:
        return await operation()
    except GatewayInputError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    except GatewayUpstreamError as exc:
        return web.json_response({"message": str(exc)}, status=exc.status)


def register_model_gateway_routes(app: web.Application, *, gateway: ModelGateway | None = None,
                                  include_model_routes: bool = True) -> ModelGateway:
    gateway = gateway or ModelGateway()

    async def startup(_: web.Application) -> None:
        await gateway.open()

    async def cleanup(_: web.Application) -> None:
        await gateway.close()

    async def status(request: web.Request) -> web.Response:
        return web.json_response({"data": await gateway.statuses()})

    async def chat(request: web.Request) -> web.Response:
        async def operation():
            body = await read_json(request)
            return web.json_response(await gateway.chat(body, request.match_info["model"]))
        return await handle_gateway_error(operation)

    async def stateless_chat(request: web.Request) -> web.Response:
        async def operation():
            body = await read_json(request)
            prompt = body.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise GatewayInputError("prompt 必须是非空字符串")
            allowed = {"prompt", "model", "maxTokens", "timeoutMs", "thinkingDepth", "temperature",
                       "responseFormat"}
            unknown = set(body) - allowed
            if unknown:
                raise GatewayInputError("请求包含不支持的字段：" + ", ".join(sorted(unknown)))
            upstream_body = {"messages": [{"role": "user", "content": prompt.strip()}],
                             "stream": False, "max_tokens": body.get("maxTokens", 2048),
                             "chat_template_kwargs": {"enable_thinking": False}}
            if body.get("responseFormat") == "json_object":
                upstream_body["response_format"] = {"type": "json_object"}
            elif body.get("responseFormat") not in (None, "text"):
                raise GatewayInputError("responseFormat 仅支持 text 或 json_object")
            if "temperature" in body:
                upstream_body["temperature"] = body["temperature"]
            result = await gateway.chat(upstream_body, body.get("model") or PUBLIC_MODEL_ID)
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise GatewayUpstreamError(502, "模型回答因长度限制未完成，请重试")
            message = choice.get("message") or {}
            text = message.get("content") or message.get("reasoning_content") or message.get("reasoning") or ""
            if not isinstance(text, str) or not text.strip():
                raise GatewayUpstreamError(502, "模型服务返回内容为空")
            return web.json_response({"success": True, "data": {
                "text": text.strip(), "model": result.get("model", PUBLIC_MODEL_ID),
                "usage": result.get("usage"), "toolCalls": [],
            }})
        return await handle_gateway_error(operation)

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    for prefix in ("/api/crowdSim", "/crowdSim"):
        if include_model_routes:
            app.router.add_get(prefix + "/models/status", status)
            app.router.add_post(prefix + "/models/{model}/chat/completions", chat)
        app.router.add_post(prefix + "/llm/chat", stateless_chat)
    return gateway
