"""通用的 YAML frontmatter + markdown body 解析。

供 agent_loader 与 skill_loader 共用，避免重复实现与循环依赖。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """解析 YAML frontmatter + markdown body，返回 (frontmatter_dict, body)。

    无 frontmatter（未找到 --- ... --- 围栏）时 raise ValueError。
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise ValueError(
            "无法解析文件: 未找到 YAML frontmatter (--- ... ---)"
        )
    frontmatter = yaml.safe_load(match.group(1)) or {}
    body = match.group(2).strip()
    return frontmatter, body


def read_frontmatter_file(path: str | Path) -> tuple[dict, str]:
    """读取文件并解析 frontmatter，返回 (frontmatter_dict, body)。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    content = path.read_text(encoding="utf-8")
    return parse_frontmatter(content)
