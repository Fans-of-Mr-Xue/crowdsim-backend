"""DeepSeek configuration loading for the standalone decision skill."""

import json
import math
from pathlib import Path
from typing import Any, TypedDict


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")


class DeepSeekConfigError(ValueError):
    """The local DeepSeek configuration is missing or invalid."""


class DeepSeekConfig(TypedDict):
    """Validated settings used by the DeepSeek client."""

    api_key: str
    base_url: str
    model: str
    timeout_seconds: float
    max_tokens: int
    temperature: float


def _required_text(settings: dict[str, Any], name: str) -> str:
    value = settings.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DeepSeekConfigError(
            f"DeepSeek config field '{name}' must be a non-empty string"
        )
    return value.strip()


def _finite_number(settings: dict[str, Any], name: str) -> float:
    value = settings.get(name)
    if isinstance(value, bool):
        raise DeepSeekConfigError(f"DeepSeek config field '{name}' must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DeepSeekConfigError(
            f"DeepSeek config field '{name}' must be a number"
        ) from exc
    if not math.isfinite(number):
        raise DeepSeekConfigError(f"DeepSeek config field '{name}' must be finite")
    return number


def load_deepseek_config(
    path: str | Path | None = None,
    *,
    require_api_key: bool = True,
) -> DeepSeekConfig:
    """Load and validate a plaintext JSON config file.

    ``require_api_key=False`` is useful for inspecting non-secret settings before
    the local key has been filled in. Network calls should always use the default
    value, which rejects an empty API key.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        with config_path.open(encoding="utf-8") as stream:
            settings = json.load(stream)
    except OSError as exc:
        raise DeepSeekConfigError(
            f"DeepSeek config could not be read: {config_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DeepSeekConfigError(
            f"DeepSeek config is not valid JSON: {config_path}"
        ) from exc

    if not isinstance(settings, dict):
        raise DeepSeekConfigError("DeepSeek config must contain a JSON object")

    api_key = settings.get("api_key", "")
    if not isinstance(api_key, str):
        raise DeepSeekConfigError("DeepSeek config field 'api_key' must be a string")
    api_key = api_key.strip()
    if require_api_key and not api_key:
        raise DeepSeekConfigError(
            f"DeepSeek API key is empty; fill in 'api_key' in {config_path}"
        )

    base_url = _required_text(settings, "base_url").rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise DeepSeekConfigError(
            "DeepSeek config field 'base_url' must be an HTTP(S) URL"
        )

    timeout_seconds = _finite_number(settings, "timeout_seconds")
    if timeout_seconds <= 0:
        raise DeepSeekConfigError(
            "DeepSeek config field 'timeout_seconds' must be greater than 0"
        )

    max_tokens_value = settings.get("max_tokens")
    if isinstance(max_tokens_value, bool) or not isinstance(max_tokens_value, int):
        raise DeepSeekConfigError(
            "DeepSeek config field 'max_tokens' must be an integer"
        )
    if max_tokens_value <= 0:
        raise DeepSeekConfigError(
            "DeepSeek config field 'max_tokens' must be greater than 0"
        )

    temperature = _finite_number(settings, "temperature")
    if not 0.0 <= temperature <= 2.0:
        raise DeepSeekConfigError(
            "DeepSeek config field 'temperature' must be between 0 and 2"
        )

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": _required_text(settings, "model"),
        "timeout_seconds": timeout_seconds,
        "max_tokens": max_tokens_value,
        "temperature": temperature,
    }
