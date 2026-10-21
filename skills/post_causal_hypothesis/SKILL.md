---
name: post_causal_hypothesis
description: 结合案例、因果存储和数据提出关键节点假设与 PC 候选。
when_to_use: 数据已校验、反事实方案尚未设计时。
phases: [post]
modes: [plan]
---

# 关键节点假设

1. 用 `serch_case_library` 与 `query_causal_storage` 查找同场景、区域、时间窗口和动作的可追溯记录；服务未接入时标记“知识来源缺失”。
2. 结合当前数据列出关键节点、可能的混杂因素、预期方向、时间先后和可验证指标。每条假设标注来源：当前数据、案例类比、因果存储或模型推测。
3. 用 `check_causal_readiness` 检查独立样本、完整数值列、样本量及适用性；通过后才用 `run_pc_analysis`。PC 图只写“候选关联结构”，保留未定向边与检验限制。连续帧/重叠窗口不充当独立重复。
4. 若 PC 不可运行，保留“未验证假设”和原因，继续提出以独立 SUMO 种子及配对干预验证的实验方案。
