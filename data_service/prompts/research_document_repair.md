输入包含验证错误与上一份 JSON。修正字段类型、可选值、字符长度和总输出长度，继续遵守上述全部要求。knowledge_types 必须是仅含指定英文标识的数组。压缩冗长总结而不删改关键事实；无法保留连续原文依据的预案或法规判定应取消，对应 evidence 返回空字符串。只返回修正后的 JSON。

regulation 与 emergency_plan 不得同时出现；保留 regulation 时删除 emergency_plan，plan_evidence 返回空字符串。
