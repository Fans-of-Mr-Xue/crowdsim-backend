---
name: post_causal_report
description: 汇总数据、方案、运行与三段分析生成可追溯事后报告。
when_to_use: 仿真结果和可用分析均已读取，准备交付报告时。
phases: [post]
modes: [plan]
---

# 报告结构

按[执行与分析共同契约](../../docs/post_execution_analysis_contract.md)基于真实封存结果整理报告。当前没有generate_causal_report执行或报告保存端点；输出文字时标为报告草稿，不称已持久化。当前/post-review/chat不接收分析/报告任务，不能新增task或输出白名单外的报告对象。

1. 任务目标、用户子问题、数据来源、`datasetId` 与版本、事实与合成数据边界。
2. 关键节点假设、案例/因果存储来源、PC 适用性与候选图状态。
3. 锁定方案：reviewPlanVersion、F-00、各CF及来源设计ID、实际参照、动作和固定项、主要终点与判断条件、种子、模型/几何/规则版本。
4. 真实任务进度、运行 ID、失败及缺失清单。
5. 观察分布、OFAT配对效应、机制消融三段结果；保留n、pairCount、estimate/ci95、指标/分析版本、单位、runId/resultId。小样本、失败、校准残差、右删失与极端值如实列出。
6. 区分策略/模块效应、PC候选图与尚未验证的作用路径。当前方向核验未接入，不报告interventionSupportedDirections；结论限定模型和工程映射。
7. 证据可取回情况：数据库/API中的数据、轨迹和输入引用，与仅保留在本机的原始输出/日志分开说明。失败批次的部分分析、恢复及独立复现尚有缺口，不写成已完成能力。

报告不得把草案、推算进度、PC 候选边或缺失的机制事件写成既成结果。
