---
name: post_causal_verify
description: 核对事后候选图、配对效应及隔离证据，区分模块效应与尚未实现的因果方向核验。
when_to_use: 已取得真实 F-00/CF 运行结果和候选图时。
phases: [post]
modes: [plan]
---

# 因果核验

按[执行与分析共同契约](../../docs/post_execution_analysis_contract.md)核对当前结果。

1. 分别记录：PC候选边及collider/undirected标记；组实际参照和控制差异；同种子pairCount/seeds/estimate/ci95；真实事件及版本。计划数从任务读取，缺失原因从运行详情和measurement核对。
2. 当前workers分析器不返回interventionSupportedDirections，services/causal_analysis.py未接入当前执行器。报告“方向核验尚未接入”；不能借用旧实现的字段，不能把非零配对区间或manipulatedFeatureId自动变成因果边方向。
3. 允许描述“在此模型和配置下，改变某控制参数的配对指标效应”及“预定义模块的消融效应”。PC的collider标记仍属算法假设下候选方向，不是经干预验证的方向；不能从策略效应推出指定密度/速度特征间的直接因果关系。
4. 微观作用路径需要真实触发/接收/遵从/改道/到达证据；当前hold/release及编译出口分配不构成完整链。所需扩展列入SUMO对接清单。真实世界因果外推另需独立证据。
