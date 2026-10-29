---
name: post_data_assessment
description: 接入、解析和校验上传数据或事中事件数据。
when_to_use: 用户提供数据集、附件或事中事件 ID 后。
phases: [post]
modes: [plan]
---

# 数据接入与评估

以[执行与分析共同契约](../../docs/post_execution_analysis_contract.md)为执行前提和工具边界。当前对话只核对已有接入回执，公开资料任务可用collectedEvidence；下列接入操作由现有页面/HTTP服务执行，不把工具名称当成已调用。

1. 先询问目标、子问题、来源及观察窗口。缺项则向用户补问。
2. 上传文件先解析成 `name`、`sourceKind`（`observed` 或 `synthetic_reference`）、`sourceNote`、`observations`；每条记录使用 `timeSeconds`、`regionId`、`population`，可带 `density`、`meanSpeed`、`pressureProxy`、`sourceTime`、`state`。原件可经 `rawFile` 保存，不能仅把本机路径当上传内容。事中来源只引用实际读取的报告和选定轮次，不猜测快照字段或数据。
3. 由数据接收接口校验时间、区域、重复记录、数值单位、缺失与来源，成功登记后记录datasetId与SHA-256版本（数据接口version，在方案中为datasetVersion），通过详情和timeline复核。缺少回执时不声称已保存。
4. 输出数据摘要：来源、版本、时间范围、区域、样本数、缺失字段、质量问题、是否合成。数据集是连续时间线时，不称其相邻帧为独立样本。
5. 基线接入支持独立上传、事中已完成且观测有效的单个轮次、公开网络资料。事中轮次保留conversationId/experimentId/runId/seed/requirementId/modelId等绑定；未测的压力或其他指标保留null，不从平均值反推。用户确认的是基线数据引用；F-00仍由锁定模型完整重跑，不是把上传帧直接当作SUMO初态。
6. 公开资料只整理事件事实与来源；缺乏标准数值时间线时仍可设计探索方案，但标明数据缺口。读取与检索只使用后端启用的能力；不能自报联网成功。不得通过模型答复给数值时间线补值。
7. 用户目的可参考风险演化、措施效果、作用机制；规范为“在某区域与时窗，以某事实基线对比某变化，观察某指标，用于某判断”。输出 proposed_goal 与 missing_fields，待用户明确确认后采用。建议的指标或用途必须标为候选。
8. 独立区分“数据有效”“用户确认”“执行输入齐全”“模型已校准”。当前纪念塔执行需metadata.sourceRecords及durationSeconds，普通时间线或事中报告不一定满足。完整工作台模板为19800秒；缺少执行流量时要求补规范输入，不让模型补写个人轨迹。校准分数未测保持null；参考与测量范围不一致时先说明对应关系。
