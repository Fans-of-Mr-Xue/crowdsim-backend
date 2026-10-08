"""Strict, reusable validation helpers for the v1 HTTP contract."""

from __future__ import annotations

import math
import re
from typing import Any


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, field: str | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.field = code, message, status, field


def fail(field: str, message: str, code: str = "VALIDATION_ERROR") -> None:
    raise ApiError(code, message, field=field)


def object_(value: Any, field: str, *, keys: set[str] | None = None) -> dict:
    if not isinstance(value, dict):
        fail(field, "必须是 JSON 对象")
    if keys is not None:
        unknown = set(value) - keys
        if unknown:
            fail(field, f"未知字段: {', '.join(sorted(unknown))}")
    return value


def text_(value: Any, field: str, *, min_len: int = 1, max_len: int = 120,
          pattern: str | None = None) -> str:
    if not isinstance(value, str) or not min_len <= len(value.strip()) <= max_len:
        fail(field, f"必须是长度 {min_len}—{max_len} 的字符串")
    value = value.strip()
    if pattern and re.fullmatch(pattern, value) is None:
        fail(field, "格式不正确")
    return value


def integer(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        fail(field, f"必须是 {low}—{high} 的整数")
    return value


def number(value: Any, field: str, low: float = 0.0, high: float = 1e12) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(field, "必须是有限数值")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        fail(field, f"必须是 {low}—{high} 的有限数值")
    return result


def array(value: Any, field: str, low: int, high: int) -> list:
    if not isinstance(value, list) or not low <= len(value) <= high:
        fail(field, f"必须是长度 {low}—{high} 的数组")
    return value


def choice(value: Any, field: str, values: set[str]) -> str:
    if not isinstance(value, str) or value not in values:
        fail(field, f"必须是以下值之一: {', '.join(sorted(values))}")
    return value


def optional_number(value: Any, field: str, low: float = 0.0,
                    high: float = 1e12) -> float | None:
    return None if value is None else number(value, field, low, high)


def reject_secrets(value: Any, field: str = "body") -> None:
    """Keep passwords and API tokens out of post-analysis persistence."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {"password", "apikey", "secret", "authorization", "sshkey"} or normalized.endswith("token"):
                fail(f"{field}.{key}", "凭据只能配置在后端环境中", "SECRET_FIELD_FORBIDDEN")
            reject_secrets(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_secrets(item, f"{field}[{index}]")


def metric_value(value: float | None, unit: str, definition_version: str,
                 *, missing_reason: str | None = None) -> dict:
    if value is None and not missing_reason:
        raise ValueError("missing metrics require missing_reason")
    return {"value": value, "unit": unit, "definitionVersion": definition_version,
            "missingReason": missing_reason if value is None else None}
