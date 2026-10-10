from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .context import TurnContext


class Tool:
    """Agent 可调用的工具基类

    description 是模型了解工具的主要渠道（通过 function calling schema 传递）。
    对标 Claude Code 的 tool.prompt()，应包含：功能 → 使用场景 → 工具区分 → 参数 → 注意事项。
    """

    name: str = ""
    description: str = ""
    input_schema: dict = {}
    category: str = ""  # 工具分组：情报检索/知识库/实验管理/因果分析/实验设计/决策剧场/态势分析/知识工程/项目管理
    readonly: bool = False  # True = 可查看 schema 但不可执行（执行层拦截）

    def execute(self, context: TurnContext, params: dict) -> str:
        raise NotImplementedError

    def service(self, context: TurnContext, name: str):
        """取运行期能力（见 harness/services.py）；未装配时抛 ServiceUnavailable。"""
        return context.require(name)

    def _summary(self) -> str:
        """提取 description 的第一句作为一句话功能摘要"""
        desc = self.description or ""
        # 取第一个句号或换行前的内容
        for sep in ("。", "\n"):
            idx = desc.find(sep)
            if idx > 0:
                return desc[:idx]
        return desc

    def _format_params_short(self) -> str:
        """生成简洁参数签名：param1, param2?=默认"""
        required = self.input_schema.get("required", [])
        props = self.input_schema.get("properties", {})
        if not props:
            return ""
        parts = []
        for key, meta in props.items():
            if key in required:
                parts.append(key)
            else:
                default = meta.get("default")
                if default is not None:
                    parts.append(f"{key}?={default}")
                else:
                    parts.append(f"{key}?")
        return ", ".join(parts)


class ToolRegistry:
    """工具注册中心"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool from this request-scoped registry when it is unavailable."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tools_prompt(self) -> str:
        """生成分类工具速查表（简洁列表，详细用法见 function definition 的 description）

        按 category 分组，每组输出：
        ## 分类名
        - `tool(params?)` — 一句话功能摘要
        """
        if not self._tools:
            return ""

        # 按 category 分组
        groups: dict[str, list[Tool]] = {}
        for tool in self._tools.values():
            cat = tool.category or "其他"
            groups.setdefault(cat, []).append(tool)

        # 按分类名排序，每组内按工具名排序
        sections = ["## 可用工具", ""]
        for cat in sorted(groups.keys()):
            tools = sorted(groups[cat], key=lambda t: t.name)
            sections.append(f"### {cat}")
            for t in tools:
                params = t._format_params_short()
                sig = f"`{t.name}({params})`" if params else f"`{t.name}`"
                summary = t._summary()
                sections.append(f"- {sig} — {summary}")
            sections.append("")
        return "\n".join(sections)

    def to_openai_tools(self) -> list[dict]:
        """生成 OpenAI 原生 function calling 格式的工具列表"""
        result = []
        for tool in self._tools.values():
            result.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            })
        return result
