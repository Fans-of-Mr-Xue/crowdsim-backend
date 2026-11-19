"""Configured local model endpoints, on-demand availability and chat forwarding."""
import asyncio
import os
from datetime import datetime, timezone

import aiohttp
from .llm import ModelError
from ..config import load_database_env

MODEL_SPECS = (
    ('GLM-5.3-Flash-8800', 'GLM-5.3 Flash（8800）', 'GLM_MAIN', 8800, 'GLM-5.3-Flash'),
    ('Qwen3.8-Flash-Next', 'Qwen3.8 Flash Next', 'QWEN', 8800, 'qwen3.8-flash-next'),
    ('DiffusionGemma', 'DiffusionGemma 26B', 'DIFFUSIONGEMMA', 8500, 'diffusiongemma-26B-A4B-it:q4'),
)


def model_configs():
    load_database_env()
    providers = {identifier: {
        'id': identifier, 'label': label, 'port': int(os.environ.get(f'CROWDSIM_{prefix}_LOCAL_PORT', port)),
        'model': os.environ.get(f'CROWDSIM_{prefix}_MODEL', name),
        'base_url': os.environ.get(f'CROWDSIM_{prefix}_BASE_URL', f"http://127.0.0.1:{os.environ.get(f'CROWDSIM_{prefix}_LOCAL_PORT', port)}/v1").rstrip('/'),
        'api_key': os.environ.get(f'CROWDSIM_{prefix}_API_KEY', os.environ.get('CROWDSIM_MODEL_API_KEY', '') if prefix in ('QWEN', 'GLM_MAIN') else ''),
    } for identifier, label, prefix, port, name in MODEL_SPECS}
    # Historical GLM selections now use the main endpoint; no Tabby tunnel.
    providers['GLM-5.3-Flash'] = {**providers['GLM-5.3-Flash-8800'], 'id': 'GLM-5.3-Flash'}
    return providers


def selected_provider(identifier, providers):
    """A discovered model may only select an existing configured endpoint."""
    if identifier in providers:
        return providers[identifier], False
    if isinstance(identifier, str) and '::' in identifier:
        endpoint, model = identifier.split('::', 1)
        if endpoint in providers and model and len(model) <= 256 and not any(ord(c) < 32 for c in model):
            return {**providers[endpoint], 'id': identifier, 'label': model, 'model': model}, True
    raise ValueError('请选择已配置或已发现的模型')


class ModelGateway:
    def __init__(self):
        self.providers = model_configs()
        self.session = None

    async def open(self):
        self.session = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=180))

    async def close(self):
        if self.session:
            await self.session.close()

    @staticmethod
    def headers(provider):
        return {'Authorization': f"Bearer {provider['api_key']}"} if provider['api_key'] else {}

    async def availability(self, provider):
        info = {key: provider[key] for key in ('id', 'label', 'model', 'port')}
        try:
            async with self.session.get(provider['base_url'] + '/models', headers=self.headers(provider), timeout=aiohttp.ClientTimeout(total=4)) as response:
                if response.status != 200:
                    return {**info, 'available': False, 'message': f'模型接口返回 HTTP {response.status}'}
                payload = await response.json(content_type=None)
            names = [item['id'] for item in payload.get('data', []) if isinstance(item, dict) and isinstance(item.get('id'), str)]
            found = any(name.lower() == provider['model'].lower() for name in names)
            return {**info, 'available': found, 'message': '可达' if found else '接口可达，但未找到配置的模型', 'models': names, 'checkedAt': datetime.now(timezone.utc).isoformat()}
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
            return {**info, 'available': False, 'message': '模型服务不可达，请检查服务和端口映射'}

    async def status(self):
        # No timer/startup probing: the frontend calls this only when its menu opens.
        return await asyncio.gather(*(self.availability(provider) for provider in self.providers.values()))

    async def chat(self, identifier, payload, *, concise_json=False):
        provider = self.providers.get(identifier)
        if provider is None:
            raise ValueError('请选择已配置的模型')
        if not isinstance(payload, dict) or payload.get('stream') not in (None, False):
            raise ValueError('模型请求必须为 JSON 对象，当前仅支持非流式响应')
        allowed = {'messages', 'stream', 'max_tokens', 'temperature', 'top_p', 'response_format', 'tools', 'tool_choice', 'chat_template_kwargs', 'reasoning_budget_tokens'}
        if set(payload) - allowed:
            raise ValueError('模型请求包含不支持的参数')
        messages = payload.get('messages')
        if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) or m.get('role') not in ('system', 'user', 'assistant', 'tool') for m in messages):
            raise ValueError('请提供有效的对话消息')
        tokens = payload.get('max_tokens', 2048)
        if type(tokens) is not int or not 1 <= tokens <= 32768:
            raise ValueError('max_tokens 必须为 1–32768 的整数')
        body = {**payload, 'model': provider['model'], 'stream': False}
        if identifier != 'Qwen3.8-Flash-Next':
            body.pop('chat_template_kwargs', None)
        if concise_json:
            # Page edits need a complete JSON answer, rather than a reasoning
            # trace consuming the answer budget. Keep this policy separate from
            # ordinary chat and apply only to providers with known support.
            if identifier in ('GLM-5.3-Flash-8800', 'GLM-5.3-Flash'):
                body.update(enable_thinking=False, reasoning_budget_tokens=0)
                body['chat_template_kwargs'] = {'enable_thinking': False}
            elif identifier == 'Qwen3.8-Flash-Next':
                body['chat_template_kwargs'] = {**body.get('chat_template_kwargs', {}), 'enable_thinking': False}
        try:
            async with self.session.post(provider['base_url'] + '/chat/completions', json=body, headers=self.headers(provider)) as response:
                if response.status != 200:
                    raise ModelError(f"{provider['label']} 返回 HTTP {response.status}，请检查模型参数或认证配置")
                return await response.json(content_type=None)
        except ModelError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise ModelError(f"{provider['label']} 服务不可达或响应超时，请检查模型服务和 {provider['port']} 端口映射") from None
