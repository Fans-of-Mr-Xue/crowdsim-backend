"""Generation limits for the configured local GLM/Qwen conversation services.

Short per-agent decisions deliberately do not use this conversation policy.
"""

MAX_OUTPUT_TOKENS = 3000
REASONING_BUDGET_TOKENS = 512
_BUDGETED_MODELS = {
    "glm-5.3-flash", "glm-5.3-flash-8800", "glm-5.3-flash-exl3-4.05bpw",
    "qwen3.8-flash-next", "qwen3.8-27b:q8",
}


def generation_options(model, max_tokens, extra_body=None):
    options = dict(extra_body or {})
    if str(model).strip().lower() not in _BUDGETED_MODELS:
        return {**options, "max_tokens": max_tokens}
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("模型输出 token 上限必须为正整数")
    reasoning = options.get("reasoning_budget_tokens")
    if reasoning is None:
        reasoning = REASONING_BUDGET_TOKENS
    if type(reasoning) is not int or reasoning < 0:
        raise ValueError("模型推理预算必须为非负整数")
    return {
        **options,
        "max_tokens": min(max_tokens, MAX_OUTPUT_TOKENS),
        "reasoning_budget_tokens": min(reasoning, REASONING_BUDGET_TOKENS),
        "chat_template_kwargs": {**options.get("chat_template_kwargs", {}), "enable_thinking": False},
    }
