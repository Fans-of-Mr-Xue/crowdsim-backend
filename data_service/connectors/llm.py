"""Shared model adapter for data imports; callers do not depend on Qwen APIs."""
from __future__ import annotations

import json
import os

import aiohttp

from ..config import load_database_env


class ModelError(Exception):
    pass


class JsonModelClient:
    def __init__(self):
        load_database_env()
        self.base_url = os.environ.get("CROWDSIM_LLM_BASE_URL", "http://127.0.0.1:8800/v1").rstrip("/")
        self.model = os.environ.get("CROWDSIM_LLM_MODEL", "qwen3.8-flash-next")
        self.api_key = os.environ.get("CROWDSIM_MODEL_API_KEY", "")
        self.extra = json.loads(os.environ.get("CROWDSIM_LLM_EXTRA_BODY", '{"chat_template_kwargs":{"enable_thinking":false}}'))
        if not isinstance(self.extra, dict) or set(self.extra) & {"messages", "model", "stream"}:
            raise ValueError("CROWDSIM_LLM_EXTRA_BODY 必须是对象，不能覆盖 messages、model 或 stream")
        self.max_output_tokens = self.extra.get("max_tokens", 3000)
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError("模型 max_tokens 必须是正整数")
        self.session = None

    async def open(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180), trust_env=False)

    async def close(self):
        if self.session is not None:
            await self.session.close()

    async def extract(self, system, source):
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {"temperature": 0, "max_tokens": self.max_output_tokens, **self.extra,
                   "model": self.model, "stream": False,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": source}]}
        try:
            async with self.session.post(self.base_url + "/chat/completions", json=payload, headers=headers) as response:
                if response.status != 200:
                    raise ModelError(f"模型服务返回 HTTP {response.status}，请检查模型名称、接口配置或认证")
                result = await response.json()
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ModelError("模型输出被截断，请重试或调整模型输出配置")
            content = choice["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except ModelError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise ModelError("无法连接模型服务或请求超时，请检查 8800 隧道和模型配置") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("模型未返回有效的 JSON 元数据，请重试") from None
