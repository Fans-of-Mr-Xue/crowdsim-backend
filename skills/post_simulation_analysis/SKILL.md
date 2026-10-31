---
name: post_simulation_analysis
description: 跟踪真实任务并读取观察、干预和机制三段分析数据。
when_to_use: 方案已提交，需查看进度或解释仿真结果时。
phases: [post]
modes: [plan]
---

# 仿真进度与分析

按[执行与分析共同契约](../../docs/post_execution_analysis_contract.md)读取当前 workers 路径的结果，不使用另一套 services 分析协议。对话本身没有任务/结果查询工具；仅解释实际提供的回执，缺少数据时列出需要读取的HTTP资源。

1. 用 `/experiments/{id}`、`/events`、`/runs` 核对真实状态和次数。结果接口仅在 completed/partial 且有resultId时可读；当前执行器失败/取消会停止，不封存部分分析，重启只标记interrupted。保留已完成运行的证据，不声称支持自动补跑或分析恢复。
2. 对齐dataset版本、reviewPlanVersion、experimentId、planId、sourceGroupId、referenceGroupId、seed、runId、resultId。所有读取沿用owner/workspace。轨迹通过 `/runs/{id}/trajectory` 分页，实际事件和校准明细通过运行详情读取。
3. 观察读取metrics.distributions的n/mean/std和观察组效应；干预读取对应组与实际参照的同种子差；机制读取相对完整观察组的消融效应。effect字段为pairCount/seeds/estimate/ci95，差值为候选减参照。不要把单组n当配对数，或补出旧字段validPairs/plannedPairs/missingReason。
4. 至少2对才给2000次配对Bootstrap区间，不足20对为descriptive_only；估计为空结合运行状态和删失信息解释。computed仅表示计算，不表示假设成立。指标单位、指标版本与分析版本分别保留。
5. 当前七项主要指标是全程全局汇总，主动停留计入速度和拥堵；全部到达时间从20:00计，未完成为null。压力是密度乘速度向量方差代理。PC特征窗口不自动成为主要终点窗口；其速度帧均值与全程人员—时间加权速度不同。
6. PC候选图位于causal-graph及intervention.causalGraph；独立F-00样本不足、常量、奇异检验状态照实呈现。当前没有interventionSupportedDirections。模块消融可分析，消息/遵从/实际改道链缺失时保留“作用路径未验证”，不把整个机制section误称为不可用。
7. 展示人数校准残差、到达/计划人数、源数据收支残差和指标区分能力；持续堵塞、全删失或极端网格值先给出诊断项。解释仅基于同一封存结果，不声称已调用generate_*工具或生成了已保存报告。
