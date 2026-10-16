"""按当前 Agent 可见范围展开中文 Skill 指引。"""

from __future__ import annotations

import json

from .tool_registry import Tool


class SkillTool(Tool):
    """加载并展开一个 MACE 技能（Skill）的完整执行指引。

    完整指引仅从调用上下文中已筛选的技能集合读取。
    """

    name = "skill"
    category = "元能力"
    readonly = False
    description = (
        "加载并展开一个 MACE 技能（Skill）的完整执行指引。"
        "未加载时只能看到技能摘要；调用后返回完整指引。\n\n"
        "可用技能清单见系统提示的「## 可用技能」段落。\n\n"
        "参数：\n"
        "- skill（必填）：技能名（来自系统提示的技能清单）\n"
        "- args（可选）：传给技能的自由文本上下文"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "技能名（来自系统提示的技能清单）",
            },
            "args": {
                "type": "string",
                "description": "传给技能的自由文本上下文，可选",
            },
        },
        "required": ["skill"],
    }

    def execute(self, context, params) -> str:
        registry = getattr(context, "skill_registry", None)
        if registry is None:
            return json.dumps({"success": False, "error": {"code": "SKILL_REGISTRY_UNAVAILABLE"}}, ensure_ascii=False)
        skill = registry.get(params.get("skill", ""))
        if skill is None:
            return json.dumps({"success": False, "error": {"code": "SKILL_NOT_ALLOWED"}}, ensure_ascii=False)
        return json.dumps({"success": True, "skill": skill.name,
                           "instruction": skill.build_prompt(args=params.get("args", ""),
                                                             mode=getattr(context, "mode", None),
                                                             phase=getattr(context, "phase", None))},
                          ensure_ascii=False)
