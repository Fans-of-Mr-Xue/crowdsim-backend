"""Minimal asynchronous client for the DeepSeek Chat Completions API."""

from pathlib import Path
from typing import Sequence

import httpx

from .config import DeepSeekConfig, load_deepseek_config
from .prompts import ChatMessage


class DeepSeekClientError(RuntimeError):
    """Base error raised by the DeepSeek transport layer."""


class DeepSeekRequestError(DeepSeekClientError):
    """The request could not be completed successfully."""


class DeepSeekResponseError(DeepSeekClientError):
    """DeepSeek returned an unreadable or incomplete response."""


class DeepSeekClient:
    """Send pre-built messages to DeepSeek and return raw model content."""

    def __init__(
        self,
        config: DeepSeekConfig | None = None,
        *,
        config_path: str | Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if config is not None and config_path is not None:
            raise ValueError("pass either config or config_path, not both")
        self.config = (
            dict(config)
            if config is not None
            else load_deepseek_config(config_path)
        )
        self._transport = transport

    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        """Return ``choices[0].message.content`` without parsing its JSON body."""
        if not messages:
            raise ValueError("at least one chat message is required")

        request_url = f"{self.config['base_url']}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config['api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config["model"],
            "messages": [dict(message) for message in messages],
            "stream": False,
            "max_tokens": self.config["max_tokens"],
            "temperature": self.config["temperature"],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.config["timeout_seconds"],
                transport=self._transport,
            ) as http_client:
                response = await http_client.post(
                    request_url,
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise DeepSeekRequestError("DeepSeek request timed out") from exc
        except httpx.RequestError as exc:
            raise DeepSeekRequestError("DeepSeek request failed") from exc

        if response.is_error:
            raise DeepSeekRequestError(
                f"DeepSeek returned HTTP status {response.status_code}"
            )

        try:
            response_data = response.json()
        except ValueError as exc:
            raise DeepSeekResponseError("DeepSeek response is not valid JSON") from exc

        try:
            content = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekResponseError(
                "DeepSeek response does not contain choices[0].message.content"
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise DeepSeekResponseError("DeepSeek returned empty message content")
        return content.strip()
