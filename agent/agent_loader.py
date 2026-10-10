"""Agent 定义加载器 — 读取 .agent.md 文件，解析为 AgentDefinition

每个 .agent.md 文件包含:
  - YAML frontmatter: name, description, mode, tools, agents, readonly_tools
  - Markdown body: system prompt

用法:
    from harness.agent_loader import load_agent, load_agents_dir

    ask_def = load_agent("harness/agents/Ask.agent.md")
    agent_map = load_agents_dir("harness/agents/")

    # 作为脚本运行
    python -m harness.agent_loader
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import HARDENING_CLAUSE, SYSTEM_PROMPT_BASE, build_runtime_context_prompt
# 通用 frontmatter 解析（agent_loader 与 skill_loader 共用）
from ._frontmatter import parse_frontmatter as _parse_frontmatter  # noqa: F401  保留原 API 名

logger = logging.getLogger(__name__)

#: 阶段说明的唯一出处（原先 context.py 里还抄了一份一模一样的，改一处漏一处）。
#: - ask/plan/agent 是终端用户对话：需要“这个模式下我该干什么”的行为倾向；
#: - internal 是后端按次调用：调用方每轮都自带任务简报与产物结构，这里再替它断言
#:   “你正在做什么”只会和调用方口径打架（各业务模块对同一阶段的理解并不相同）。
#:   所以 internal 只拿一行事实性说明，并明确以调用方指令为准。
#: 这里**不放任何业务口径**：阶段的业务含义由各调用方在自己的指令里写。
_USER_PHASE_LABELS = {"pre": "事前研判", "during": "事中决策", "post": "事后复盘"}
_USER_PHASE_NOTES = {
    "pre": "你正在帮助用户在实验设计阶段进行情报收集和方案规划。",
    "during": "你正在帮助用户在执行阶段监控实验进展和分析中间结果。",
    "post": "你正在帮助用户分析实验结果和总结经验教训。",
}
_INTERNAL_PHASE_LABELS = {"pre": "事前", "during": "事中", "post": "事后"}


def phase_instruction(phase: Optional[str], mode: Optional[str] = None) -> str:
    """阶段段提示；阶段没给或认不出来时返回空串（不给“看起来能用”的默认值）。"""
    key = str(phase or "").strip().lower()
    if key not in _USER_PHASE_LABELS:
        return ""
    if str(mode or "").strip().lower() == "internal":
        return (
            f"\n## 当前阶段：{_INTERNAL_PHASE_LABELS[key]}（{key}，由调用方指定）\n"
            "阶段由调用方指定。本阶段做什么、产出什么结构，一律以调用方本轮指令为准。"
        )
    return f"\n## 当前阶段：{_USER_PHASE_LABELS[key]}\n{_USER_PHASE_NOTES[key]}"


@dataclass
class AgentDefinition:
    """单个 Agent 的完整定义"""

    name: str
    description: str = ""
    mode: str = ""
    tool_allowlist: list[str] = field(default_factory=list)
    readonly_tools: list[str] = field(default_factory=list)
    sub_agents: list[str] = field(default_factory=list)
    system_prompt: str = ""
    skills: list[str] = field(default_factory=list)  # 显式 opt-in 的 skill 名；空 = 不可见任何 skill

    # 原始 frontmatter 中未识别的字段
    extra: dict = field(default_factory=dict)

    def build_system_prompt(
        self,
        phase: Optional[str] = None,
        tool_registry=None,
        skill_registry=None,
        runtime_context_prompt: Optional[str] = None,
    ) -> str:
        parts = [
            SYSTEM_PROMPT_BASE,
            build_runtime_context_prompt(),
        ]
        if runtime_context_prompt:
            parts.append(runtime_context_prompt)
        parts.extend([self.system_prompt, HARDENING_CLAUSE])

        # 工具清单：当 tool_registry 存在时不再注入文本速查表，
        # 因为 OpenAI function calling schema 已通过协议层承载完整工具信息。
        # 仅在 fallback 场景（没有 registry 但 agent 声明了 allowlist）输出名称列表。
        if tool_registry is None and self.tool_allowlist:
            names = ", ".join(f"`{t}`" for t in self.tool_allowlist)
            parts.append(f"\n## 可用工具\n{names}")

        # 只读工具说明：当 agent 声明了 readonly_tools 时注入提示文本
        if self.readonly_tools:
            from harness.tools import create_default_registry
            full = create_default_registry()
            lines = ["\n## 只读工具（仅供引用，不可调用）", ""]
            lines.append("以下工具已出现在你的工具列表中（描述以 [只读] 开头），"
                         "你可以在实验方案中引用它们，但**不可调用**。"
                         "如需执行，请建议用户切换到 Agent 模式。\n")
            for name in self.readonly_tools:
                tool = full.get(name)
                if tool:
                    lines.append(f"- **{name}**: {tool.description}")
            parts.append("\n".join(lines))

        note = phase_instruction(phase, self.mode)
        if note:
            parts.append(note)

        # 可用技能摘要（渐进式披露：仅 name/desc/when_to_use/argument-hint）
        # skill_registry 为已按 mode/phase 过滤后的可见集合；agent 未声明 skills
        # （空列表）时 filter_for 返回空 registry，无摘要段。
        if skill_registry is not None:
            visible_prompt = skill_registry.get_skills_prompt()
            if visible_prompt:
                parts.append("\n" + visible_prompt)

        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "mode": self.mode,
            "tool_allowlist": self.tool_allowlist,
            "readonly_tools": self.readonly_tools,
            "sub_agents": self.sub_agents,
            "skills": self.skills,
            "system_prompt_length": len(self.system_prompt),
            "extra": self.extra,
        }


def agent_from_dict(frontmatter: dict, body: str) -> AgentDefinition:
    """从 frontmatter dict + body 构建 AgentDefinition"""
    known = {"name", "description", "mode", "tools", "agents", "readonly_tools",
             "skills", "argument-hint", "target", "disable-model-invocation", "handoffs"}
    extra = {k: v for k, v in frontmatter.items() if k not in known}

    return AgentDefinition(
        name=frontmatter.get("name", ""),
        description=frontmatter.get("description", ""),
        mode=frontmatter.get("mode", frontmatter.get("name", "").lower()),
        tool_allowlist=frontmatter.get("tools", []),
        readonly_tools=frontmatter.get("readonly_tools", []),
        sub_agents=frontmatter.get("agents", []),
        skills=frontmatter.get("skills", []),
        system_prompt=body,
        extra=extra,
    )


def load_agent(filepath: str | Path) -> AgentDefinition:
    """加载单个 .agent.md 文件"""
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Agent 定义文件不存在: {filepath}")
    content = filepath.read_text(encoding="utf-8")
    frontmatter, body = _parse_frontmatter(content)
    definition = agent_from_dict(frontmatter, body)
    logger.info("Loaded agent '%s' from %s (%d tools)",
                definition.name, filepath.name, len(definition.tool_allowlist))
    return definition


def load_agents_dir(directory: str | Path) -> dict[str, AgentDefinition]:
    """加载目录下所有 .agent.md 文件, 返回 {mode: AgentDefinition} 映射"""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Agent 目录不存在: {directory}")

    agents: dict[str, AgentDefinition] = {}
    for md_file in sorted(directory.glob("*.agent.md")):
        try:
            agent_def = load_agent(md_file)
            agents[agent_def.mode] = agent_def
        except Exception:
            logger.exception("Failed to load agent from %s", md_file)

    logger.info("Loaded %d agents from %s", len(agents), directory)
    return agents


def get_default_agents_dir() -> Path:
    """返回默认的 agents 目录路径"""
    return Path(__file__).resolve().parent / "agents"


def validate_allowlist(
    allowlist: list[str],
    available_names: set[str],
) -> dict:
    """对 agent 白名单做二分类返回。

    - ok: 可解析到的工具名（含通配符 "*"）
    - missing: 拼写错误或已隔离不复存在的工具名
    """
    ok, missing = [], []
    for name in allowlist:
        if name == "*" or name in available_names:
            ok.append(name)
        else:
            missing.append(name)
    return {"ok": ok, "missing": missing}


def audit_registry_and_agents(
    agent_defs: dict,
    available_names: set[str],
) -> list[dict]:
    """对 registry 状态 + 各 agent 白名单做一次性结构化审计。

    同时校验 tools 与 readonly_tools（历史上 readonly 漂移是静默的）。
    Caller 可遍历 events 用结构化 logger 输出，并在 missing 非空时额外 warn。
    """
    events: list[dict] = [{
        "event": "registry_loaded",
        "default_count": len(available_names),
        "default_names": sorted(available_names),
    }]
    for mode, agent_def in agent_defs.items():
        tools = validate_allowlist(agent_def.tool_allowlist, available_names)
        readonly = validate_allowlist(agent_def.readonly_tools, available_names)
        events.append({
            "event": "agent_allowlist_validated",
            "agent_name": agent_def.name,
            "mode": mode,
            "allowlist_size": len(agent_def.tool_allowlist),
            "ok_count": len(tools["ok"]),
            "missing": tools["missing"],
            "readonly_size": len(agent_def.readonly_tools),
            "readonly_missing": readonly["missing"],
        })
    return events


# ═══════════════════════════════════════════════
# 脚本入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    agents_dir = sys.argv[1] if len(sys.argv) > 1 else str(get_default_agents_dir())
    print(f"Loading agents from: {agents_dir}\n")

    agents = load_agents_dir(agents_dir)

    for mode, agent_def in agents.items():
        print(f"  [{mode}] {agent_def.name}")
        print(f"    Description: {agent_def.description}")
        print(f"    Tools: {len(agent_def.tool_allowlist)}")
        print(f"    Sub-agents: {agent_def.sub_agents or 'none'}")
        print(f"    System prompt: {len(agent_def.system_prompt)} chars")
        print()

    if not agents:
        print("No agents loaded.")
