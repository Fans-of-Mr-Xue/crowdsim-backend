---
name: post_data_assessment
description: 接入、解析和校验上传数据或事中事件数据。
when_to_use: 用户提供数据集、附件或事中事件 ID 后。
phases: [post]
modes: [plan]
---

# 数据接入与评估

1. 先用 `get_user_goal` 确认目标、子问题、来源及观察窗口。缺项则向用户补问。
2. 上传文件先解析成 `name`、`sourceKind`（`observed` 或 `synthetic_reference`）、`sourceNote`、`observations`；每条记录使用 `timeSeconds`、`regionId`、`population`，可带 `density`、`meanSpeed`、`pressureProxy`、`sourceTime`、`state`。原件可经 `rawFile` 保存，不能仅把本机路径当上传内容。事中事件用 `get_event_snapshot_by_id` 取得真实快照，不能猜测其字段或数据。
3. 使用 `validate_dataset` 校验时间、区域、重复记录、数值单位、缺失与来源。成功后使用 `register_upload_dataset`，记录 `datasetId` 与 SHA-256 `datasetVersion`；用 `inspect_dataset` 复核。
4. 输出数据摘要：来源、版本、时间范围、区域、样本数、缺失字段、质量问题、是否合成。数据集是连续时间线时，不称其相邻帧为独立样本。
5. 基线接入支持独立上传、事中已完成且观测有效的单个轮次、公开网络资料。事中轮次保留 conversationId / experimentId / runId / seed / requirementId / modelId 等绑定；未测的压力或其他指标保留 null，不从平均值反推。用户逐项核对后明确确认，才固定为 F-00。
6. 公开资料只整理事件事实与来源；缺乏标准数值时间线时仍可设计探索方案，但标明数据缺口。读取与检索只使用后端启用的能力；不能自报联网成功。不得通过模型答复给数值时间线补值。
7. 用户目的可参考风险演化、措施效果、作用机制；规范为“在某区域与时窗，以某事实基线对比某变化，观察某指标，用于某判断”。输出 proposed_goal 与 missing_fields，待用户明确确认后采用。建议的指标或用途必须标为候选。
