"""Shared model adapter for data imports; callers do not depend on Qwen APIs."""
from __future__ import annotations

import json
import os
import copy

import aiohttp

from crowdsim.domain.llm_generation import generation_options
from ..config import load_database_env


class ModelError(Exception):
    pass


class JsonModelClient:
    def __init__(self):
        load_database_env()
        from .models import model_configs
        default_provider = model_configs()['GLM-5.3-Flash-8800']
        self.base_url = os.environ.get("CROWDSIM_LLM_BASE_URL", default_provider['base_url']).rstrip("/")
        self.model = os.environ.get("CROWDSIM_LLM_MODEL", default_provider['model'])
        self.api_key = default_provider['api_key']
        self.extra = json.loads(os.environ.get("CROWDSIM_LLM_EXTRA_BODY", '{}'))
        if not isinstance(self.extra, dict) or set(self.extra) & {"messages", "model", "stream"}:
            raise ValueError("CROWDSIM_LLM_EXTRA_BODY 必须是对象，不能覆盖 messages、model 或 stream")
        self.max_output_tokens = self.extra.get("max_tokens", 3000)
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError("模型 max_tokens 必须是正整数")
        self.extra = generation_options(self.model, self.max_output_tokens, self.extra)
        self.max_output_tokens = self.extra.pop("max_tokens")
        self.session = None

    async def open(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180), trust_env=False)

    async def close(self):
        if self.session is not None:
            await self.session.close()

    def for_provider(self, identifier):
        # The task gets its own configuration while reusing the open HTTP session.
        from .models import model_configs
        provider = model_configs().get(identifier)
        if provider is None:
            raise ValueError("请选择已配置的模型")
        client = copy.copy(self)
        client.base_url = provider['base_url']
        client.model = provider['model']
        client.api_key = provider['api_key']
        extra = self.extra if identifier == 'Qwen3.8-Flash-Next' else {}
        client.extra = generation_options(client.model, client.max_output_tokens, extra)
        client.max_output_tokens = client.extra.pop('max_tokens')
        return client

    async def extract(self, system, source, *, fast_json=False):
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {"temperature": 0, "max_tokens": self.max_output_tokens, **self.extra,
                   "model": self.model, "stream": False,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": source}]}
        if fast_json:
            # A tag list needs an answer budget, not thousands of reasoning tokens.
            # Tabby exposes reasoning_budget_tokens; template flags alone do not
            # reliably stop GLM reasoning on the configured local endpoint.
            payload.update(max_tokens=1024, enable_thinking=False,
                           reasoning_budget_tokens=0, response_format={"type": "json_object"})
            payload["chat_template_kwargs"] = {**payload.get("chat_template_kwargs", {}), "enable_thinking": False}
        try:
            async with self.session.post(self.base_url + "/chat/completions", json=payload, headers=headers) as response:
                if response.status != 200:
                    raise ModelError(f"模型服务返回 HTTP {response.status}，请检查模型名称、接口配置或认证")
                result = await response.json()
            choice = result["choices"][0]
            if not fast_json and choice.get("finish_reason") == "length":
                raise ModelError("模型输出被截断，请重试或调整模型输出配置")
            content = (choice["message"].get("content") or "").strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            try:
                data = json.loads(content)
            except ValueError:
                if choice.get("finish_reason") == "length":
                    raise ModelError("模型输出被截断，请重试或调整模型输出配置") from None
                raise
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except ModelError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise ModelError(f"无法连接 {self.model} 或请求超时，请检查所选模型的端口映射和配置") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("模型未返回有效的 JSON 元数据，请重试") from None
