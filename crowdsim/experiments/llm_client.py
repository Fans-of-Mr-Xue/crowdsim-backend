"""OpenAI-compatible client used by the C2-C4 comparison controllers."""

from __future__ import annotations

import os
from typing import Any, Mapping

import httpx


class LlmUnavailableError(RuntimeError):
    """Raised when a real model call cannot be made."""


class ControlLlmClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = str(base_url or os.environ.get("CROWDSIM_LLM_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        self.api_key = str(api_key or os.environ.get("CROWDSIM_LLM_API_KEY") or "").strip()
        self.model = str(model or os.environ.get("CROWDSIM_LLM_MODEL") or "deepseek-chat").strip()
        self.transport = transport

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None):
        data = dict(config or {})
        return cls(
            base_url=data.get("baseUrl") or data.get("base_url"),
            model=data.get("model"),
        )

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        timeout: float = 30.0,
        model: str | None = None,
        max_tokens: int = 1200,
    ) -> tuple[str, dict[str, Any]]:
        if not self.api_key:
            raise LlmUnavailableError("CROWDSIM_LLM_API_KEY is not configured")
        request_model = str(model or self.model).strip()
        if not request_model:
            raise LlmUnavailableError("CROWDSIM_LLM_MODEL is not configured")
        url = self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"
        payload = {
            "model": request_model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max(1, min(4096, int(max_tokens))),
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(timeout=max(1.0, float(timeout)), transport=self.transport) as client:
                response = client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LlmUnavailableError(f"model endpoint returned HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise LlmUnavailableError(f"model endpoint is unavailable: {exc}") from exc
        data = response.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise LlmUnavailableError("model response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise LlmUnavailableError("model response content is empty")
        return content, {"model": data.get("model") or request_model, "usage": data.get("usage")}
