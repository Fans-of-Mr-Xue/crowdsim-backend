---
name: post_causal_visualization
description: 将三段分析的已计算指标与候选关系组织为前端可视化数据。
when_to_use: 需要向事后页面提供图表、指标卡和可核查解释时。
phases: [post]
modes: [plan]
---

# 可视化数据组织

按[执行与分析共同契约](../../docs/post_execution_analysis_contract.md)组织现有结果，不新增或猜造API字段。

- 观察使用metrics.distributions及observation.experiments；干预使用intervention.experiments和causalGraph；机制展示mechanism.experiments的消融效应，另行区分真实事件与未验证路径。
- 保留groupId/referenceGroupId、n或pairCount、estimate/ci95、单位、指标/分析版本与resultId。缺失值显示运行或删失依据；缺少显式原因字段时说明需查运行详情，不填零。
- PC边按collider候选/undirected显示，样本不足或退化不画成已发现的边。当前没有干预支持方向图例对应的数据，不能用策略效应替它着色。
- 明确全程全局指标与PC窗口/区域特征的区别。computed表示已计算，非通过检验。图表与文字使用同一封存结果；对话接口不直接生成这些分析对象。
