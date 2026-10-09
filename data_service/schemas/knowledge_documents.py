"""Shared validation; each library owns its fields, prompts and extraction limits."""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class DocumentProfile:
    collection: str
    endpoint: str
    label: str
    prompt_prefix: str
    fields: dict
    labels: dict
    model_limits: dict
    max_tags: int = 30
    max_tag_chars: int = 80

    def validate(self, payload, *, require_name=True, for_model=False):
        if not isinstance(payload, dict) or set(payload) - (set(self.fields) | {"tags"}):
            raise ValueError("元数据包含不支持的字段")
        limits = {**self.fields, **self.model_limits} if for_model else self.fields
        data = {}
        for key, limit in limits.items():
            value = payload.get(key, "")
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"{key} 必须是长度不超过 {limit} 的文本")
            data[key] = value.strip()
        if require_name and not data["name"]:
            raise ValueError("请输入资料名称")
        tags = payload.get("tags", [])
        if not isinstance(tags, list) or len(tags) > self.max_tags or any(not isinstance(tag, str) or len(tag) > self.max_tag_chars for tag in tags):
            raise ValueError(f"tags 最多 {self.max_tags} 项，每项须为不超过 {self.max_tag_chars} 个字符的字符串")
        data["tags"] = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
        return data

    def constraints(self):
        limits = {**self.fields, **self.model_limits}
        return "\n".join(f"- {key}（{self.labels[key]}）：字符串，最多 {limit} 个字符。" for key, limit in limits.items()) + f"\n- tags：字符串数组，最多 {self.max_tags} 项，每项最多 {self.max_tag_chars} 个字符。"

    def example(self):
        return json.dumps({**dict.fromkeys(self.fields, ""), "tags": []}, ensure_ascii=False)
