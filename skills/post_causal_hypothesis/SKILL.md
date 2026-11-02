---
name: post_causal_hypothesis
description: 结合案例、因果存储和数据提出关键节点假设与 PC 候选。
when_to_use: 数据已校验、反事实方案尚未设计时。
phases: [post]
modes: [plan]
---

# 关键节点假设

以[执行与分析共同契约](../../docs/post_execution_analysis_contract.md)为依据。

1. 只使用上下文中可追溯的当前数据、案例、因果存储或实际读取的来源；没有检索服务时标记缺口，不声称调用serch_case_library/query_causal_storage。模型推测单独标注。
2. 列出关键节点、可能混杂、预期方向、时间先后、可用指标与替代解释。先确认现有SUMO控制能否作用于该假设；认知、消息传播、伤亡及中介机制没有对应实现时保留待扩展。
3. 设计前无需先得到PC图。当前统一实验池按变量、假设和参照设计，不按三段分析分类。PC由真实批次完成后的workers/causal.py计算；仅在具备执行能力的上下文建议2—8项已选密度/速度/压力特征、区域和窗口，不运行PC。现有pc_defaults专用入口仍兼容旧干预上下文。
4. 每个完整独立F-00种子形成一行，至少20个完整样本且列有方差才可估计；连续帧、多个窗口和配对CF都不增加独立样本数。窗口可作为不同列，明确线性高斯、因果充分性和忠实性假设。
5. 没有图时按未验证假设继续设计；有图时保留算法返回的insufficient_data/degenerate_data/partially_estimable状态，collider标记仍为候选。主终点、重复数和机制证据按研究目标确定，不为得到一张PC图而改变问题。
