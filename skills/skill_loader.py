"""Skill 定义加载器 — 读取 <skill-name>/SKILL.md，解析为 SkillDefinition

每个 SKILL.md 文件包含:
  - YAML frontmatter: name, description, when_to_use, phases, modes, argument-hint
  - Markdown body: 完整执行指引（仅在 skill 工具被调用时展开注入）

对标 agent_loader.py 的结构。与 ToolRegistry 不同的是，SkillDefinition.body
在加载时即读入内存（skill 少且小，全量常驻可接受），但系统提示构建路径
（get_skills_prompt）永不触碰 body，仅暴露摘要 —— 渐进式披露。

用法:
    from harness.skill_loader import load_skills_dir, get_default_skills_dir

    skills = load_skills_dir(get_default_skills_dir())

    # 作为脚本运行
    python -m harness.skill_loader
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ._frontmatter import read_frontmatter_file

logger = logging.getLogger(__name__)

# 已知 frontmatter 字段；其余进 extra
# 注意：不提供 allowed-tools —— skill 是「如何使用工具」的指导，不是权限白名单；
# 工具可见性由 agent 的 tools/readonly_tools 白名单治理。
_KNOWN_FIELDS = {
    "name", "description", "when_to_use",
    "phases", "modes", "argument-hint",
}

# $SKILL_DIR / $ARGS 替换外的命名占位符：形如 $key
_NAMED_PLACEHOLDER_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class SkillDefinition:
    """单个 Skill 的完整定义"""

    name: str
    description: str = ""
    when_to_use: str = ""
    phases: list[str] = field(default_factory=list)  # 空 = 全 phase
    modes: list[str] = field(default_factory=list)  # 空 = 全 mode
    argument_hint: str = ""
    body: str = ""  # 完整 prompt，仅调用时展开
    source_path: Path | None = None
    extra: dict = field(default_factory=dict)

    @property
    def skill_dir(self) -> Path:
        """SKILL.md 所在目录（source_path.parent）"""
        if self.source_path is None:
            raise ValueError(f"Skill '{self.name}' 未设置 source_path")
        return self.source_path.parent

    def visible_for(self, mode: Optional[str], phase: Optional[str]) -> bool:
        """判断该 skill 在指定 mode/phase 下是否可见。

        modes/phases 为空时不过滤（即全 mode / 全 phase 可见）；非空时做包含判断。
        """
        if self.modes and mode not in self.modes:
            return False
        if self.phases and phase not in self.phases:
            return False
        return True

    def build_prompt(
        self,
        args: str = "",
        mode: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> str:
        """展开完整 body：公共 SKILL.md body + 当前 mode 专属文件后替换占位符。

        替换顺序：先 $SKILL_DIR（绝对路径），再命名占位符 $key，最后 $ARGS。
        """
        parts = [self.body]
        scoped_file = _read_mode_prompt_file(self.skill_dir, mode)
        if scoped_file:
            parts.append(scoped_file)
        prompt = "\n\n".join(p.strip() for p in parts if p and p.strip())

        # $SKILL_DIR → skill 目录绝对路径
        if "$SKILL_DIR" in prompt:
            prompt = prompt.replace("$SKILL_DIR", str(self.skill_dir.resolve()))

        # 命名占位符 $key —— 从 args 解析 "key: value; key2: value2" 风格
        named = _parse_named_args(args)
        if named:
            def _replace(m: re.Match) -> str:
                key = m.group(1)
                if key in named:
                    return str(named[key])
                return m.group(0)
            prompt = _NAMED_PLACEHOLDER_RE.sub(_replace, prompt)

        # $ARGS → 整段 args（最后替换，避免命名占位符被吃掉）
        if "$ARGS" in prompt:
            prompt = prompt.replace("$ARGS", args or "")

        return prompt

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "phases": self.phases,
            "modes": self.modes,
            "argument_hint": self.argument_hint,
            "body_length": len(self.body),
            "extra": self.extra,
        }


def _parse_named_args(args: str) -> dict[str, str]:
    """从 "key: value; key2: value2" 风格字符串解析命名参数。

    宽松解析：按 ; 或换行分条，每条取第一个冒号切分。无法解析的条目跳过。
    """
    named: dict[str, str] = {}
    if not args:
        return named
    for seg in re.split(r"[;\n]", args):
        seg = seg.strip()
        if not seg or ":" not in seg:
            continue
        key, _, value = seg.partition(":")
        key = key.strip()
        value = value.strip()
        if key and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            named[key] = value
    return named


def _safe_scope_name(value: Optional[str]) -> str:
    """把 mode 名规整为可用文件名片段。"""
    if not value:
        return ""
    value = value.strip().lower()
    if not re.match(r"^[a-z0-9_-]+$", value):
        return ""
    return value


def _read_mode_prompt_file(skill_dir: Path, mode: Optional[str]) -> str:
    """读取 skill 目录中的当前 mode 专属指引文件。

    支持两种布局：
      <skill>/plan.md
      <skill>/modes/plan.md

    文件不存在时返回空串，保持兼容仅使用 SKILL.md 的 skill。
    """
    scope = _safe_scope_name(mode)
    if not scope:
        return ""

    candidates = [
        skill_dir / f"{scope}.md",
        skill_dir / "modes" / f"{scope}.md",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8")
    return ""


def skill_from_dict(frontmatter: dict, body: str, source: Path | None = None) -> SkillDefinition:
    """从 frontmatter dict + body 构建 SkillDefinition"""
    extra = {k: v for k, v in frontmatter.items() if k not in _KNOWN_FIELDS}

    return SkillDefinition(
        name=str(frontmatter.get("name", "")).strip(),
        description=str(frontmatter.get("description", "")).strip(),
        when_to_use=str(frontmatter.get("when_to_use", "")).strip(),
        phases=_as_str_list(frontmatter.get("phases")),
        modes=_as_str_list(frontmatter.get("modes")),
        argument_hint=str(frontmatter.get("argument-hint", "")).strip(),
        body=body,
        source_path=source,
        extra=extra,
    )


def _as_str_list(value) -> list[str]:
    """把 frontmatter 中的标量/列表归一为 list[str]。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def load_skill(skill_dir: str | Path) -> SkillDefinition:
    """加载单个 skill 目录（读 skill_dir/SKILL.md）"""
    skill_dir = Path(skill_dir)
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        raise FileNotFoundError(f"SKILL.md 不存在: {skill_file}")
    frontmatter, body = read_frontmatter_file(skill_file)
    definition = skill_from_dict(frontmatter, body, source=skill_file)

    # name 缺省时回退到目录名
    if not definition.name:
        definition.name = skill_dir.name

    # 一致性校验：name 应等于目录名（偏离只 warn，不阻断）
    if definition.name != skill_dir.name:
        logger.warning(
            "Skill name '%s' 与目录名 '%s' 不一致（以 name 为准）",
            definition.name, skill_dir.name,
        )

    logger.info("Loaded skill '%s' from %s", definition.name, skill_file.name)
    return definition


def load_skills_dir(directory: str | Path) -> dict[str, SkillDefinition]:
    """扫描目录下所有 <skill-name>/SKILL.md，返回 {name: SkillDefinition}"""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Skill 目录不存在: {directory}")

    skills: dict[str, SkillDefinition] = {}
    for skill_file in sorted(directory.glob("*/SKILL.md")):
        try:
            skill_def = load_skill(skill_file.parent)
            skills[skill_def.name] = skill_def
        except Exception:
            logger.exception("Failed to load skill from %s", skill_file)

    logger.info("Loaded %d skills from %s", len(skills), directory)
    return skills


def get_default_skills_dir() -> Path:
    """返回默认的 skills 目录路径（harness/skills）"""
    return Path(__file__).resolve().parent / "skills"


# ═══════════════════════════════════════════════
# 脚本入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    skills_dir = sys.argv[1] if len(sys.argv) > 1 else str(get_default_skills_dir())
    print(f"Loading skills from: {skills_dir}\n")

    skills = load_skills_dir(skills_dir)

    for name, skill_def in skills.items():
        print(f"  [{name}]")
        print(f"    Description: {skill_def.description}")
        print(f"    When to use: {skill_def.when_to_use}")
        print(f"    Phases: {skill_def.phases or '(all)'}")
        print(f"    Modes: {skill_def.modes or '(all)'}")
        print(f"    Argument hint: {skill_def.argument_hint or '(none)'}")
        print(f"    Body: {len(skill_def.body)} chars")
        print()

    if not skills:
        print("No skills loaded.")
