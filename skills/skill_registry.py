"""Skill 注册中心 —— 对标 ToolRegistry，但生命周期/预算策略独立。

唯一与 ToolRegistry 的桥梁是 SkillTool（注册进 tool registry 提供 OpenAI
schema，实际执行由 stream_chat skill 特例分支处理）。

渐进式披露：get_skills_prompt 仅暴露 name/description/when_to_use/argument-hint
摘要并做预算截断，永不触碰 body。完整 body 仅在 skill 工具被调用时通过
SkillDefinition.build_prompt 展开。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .config import (
    SKILL_DESC_MAX_CHARS,
    SKILL_PROMPT_BUDGET,
    SKILL_WHEN_MAX_CHARS,
)
from .skill_loader import SkillDefinition

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# agent.skills 显式 opt-in 的通配符
_WILDCARD = "*"


def _truncate(text: str, max_chars: int) -> str:
    """按字符数截断，超出加省略号。"""
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return "…"
    return text[: max_chars - 1].rstrip() + "…"


class SkillRegistry:
    """Skill 注册中心"""

    def __init__(self):
        self._skills: dict[str, SkillDefinition] = {}

    def register(self, skill: SkillDefinition) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Optional[SkillDefinition]:
        """按名查找，容忍前导 /（用户可能把 /skill 当成 skill 名输入）"""
        if name is None:
            return None
        name = name.strip().lstrip("/")
        return self._skills.get(name)

    def list_skills(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    def list_names(self) -> list[str]:
        return list(self._skills.keys())

    def has_skill(self, name: str) -> bool:
        return self.get(name) is not None

    def filter_for(
        self,
        names: Optional[list[str]],
        mode: Optional[str],
        phase: Optional[str],
    ) -> "SkillRegistry":
        """按 agent 的 skills 白名单 + mode/phase 过滤，返回新的 registry。

        - names=None 或含 "*"：不按白名单过滤（仅按 mode/phase 过滤）。
        - names 为空列表：显式 opt-out → 返回空 registry（agent 不声明 skills 看不到任何 skill）。
        - names 为显式列表：只保留白名单命中且 mode/phase 可见的 skill。
        """
        filtered = SkillRegistry()

        # 空 list = 显式不授权任何 skill
        if names is not None and len(names) == 0:
            return filtered

        allow_wildcard = names is None or _WILDCARD in names
        name_set = set(names) if names else set()

        for skill in self._skills.values():
            if not allow_wildcard and skill.name not in name_set:
                continue
            if not skill.visible_for(mode, phase):
                continue
            filtered.register(skill)

        return filtered

    def get_skills_prompt(self) -> str:
        """生成系统提示摘要段「## 可用技能」。

        仅 name/description/when_to_use/argument-hint，带字符预算截断降级：
        超预算时先丢 argument-hint → 再丢 when_to_use → 整体截断。
        """
        if not self._skills:
            return ""

        # 按名排序保证稳定输出
        skills = sorted(self._skills.values(), key=lambda s: s.name)

        # 先尝试完整摘要
        full_entries = [self._format_entry(s) for s in skills]
        full_total = sum(len(e) for e in full_entries) + max(len(full_entries) - 1, 0)

        if full_total <= SKILL_PROMPT_BUDGET:
            return self._assemble(full_entries)

        # 降级一：去 argument-hint
        degraded = [self._format_entry(s, with_arg_hint=False) for s in skills]
        degraded_total = sum(len(e) for e in degraded) + max(len(degraded) - 1, 0)
        if degraded_total <= SKILL_PROMPT_BUDGET:
            logger.info(
                "skill_prompt_degraded: dropped argument_hint (full=%d, degraded=%d, budget=%d)",
                full_total, degraded_total, SKILL_PROMPT_BUDGET,
            )
            return self._assemble(degraded)

        # 降级二：去 when_to_use
        degraded2 = [self._format_entry(s, with_when=False, with_arg_hint=False) for s in skills]
        degraded2_total = sum(len(e) for e in degraded2) + max(len(degraded2) - 1, 0)
        if degraded2_total <= SKILL_PROMPT_BUDGET:
            logger.info(
                "skill_prompt_degraded: dropped when_to_use+argument_hint (full=%d, degraded=%d, budget=%d)",
                full_total, degraded2_total, SKILL_PROMPT_BUDGET,
            )
            return self._assemble(degraded2)

        # 兜底：按预算硬截断（保留尽可能多的完整条目，最后一条截断）
        logger.warning(
            "skill_prompt_truncated: names-only still over budget (full=%d, budget=%d)",
            full_total, SKILL_PROMPT_BUDGET,
        )
        header = "## 可用技能\n\n调用 `skill` 工具（参数：技能名）加载完整执行指引后再据此操作。\n\n"
        budget_for_entries = SKILL_PROMPT_BUDGET - len(header)
        kept: list[str] = []
        used = 0
        for s in skills:
            line = f"- `{s.name}`"
            if used + len(line) + 1 > budget_for_entries:
                break
            kept.append(line)
            used += len(line) + 1
        return header + "\n".join(kept)

    @staticmethod
    def _format_entry(
        skill: SkillDefinition,
        with_when: bool = True,
        with_arg_hint: bool = True,
    ) -> str:
        """格式化单条 skill 摘要：- `name`: description — when_to_use (args: hint)"""
        desc = _truncate(skill.description or "(无描述)", SKILL_DESC_MAX_CHARS)
        parts = [f"- `{skill.name}`: {desc}"]

        if with_when and skill.when_to_use:
            when = _truncate(skill.when_to_use, SKILL_WHEN_MAX_CHARS)
            parts.append(f" — {when}")

        if with_arg_hint and skill.argument_hint:
            parts.append(f" (args: {skill.argument_hint})")

        return "".join(parts)

    @staticmethod
    def _assemble(entries: list[str]) -> str:
        if not entries:
            return ""
        header = (
            "## 可用技能\n\n"
            "调用 `skill` 工具（参数：技能名）加载完整执行指引后再据此操作。\n\n"
        )
        return header + "\n".join(entries)
