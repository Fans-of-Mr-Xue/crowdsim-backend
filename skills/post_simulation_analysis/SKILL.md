---
name: post_simulation_analysis
description: 跟踪真实任务并读取观察、干预和机制三段分析数据。
when_to_use: 方案已提交，需查看进度或解释仿真结果时。
phases: [post]
modes: [plan]
---

# 仿真进度与分析

使用 `get_simulation_progress` 读取实验状态、真实完成数及事件游标。只有状态为 `completed` 或 `partial` 且存在 `resultId` 时调用 `get_simulation_results`、`analyze_simulation_results`。结果来自数据库封存记录，按 `experimentId`、`planId`、`seed`、数据版本与指标版本对齐。

观察段解释 F-00 指标与 PC 候选；干预段解释同种子配对差、置信区间、有效配对数；机制段须有规则切换、决策、消息、改道等真实事件。无机制证据时写“暂不可分析”。若用户有子问题，再分别使用 `generate_observe_answer`、`generate_intervene_answer`、`generate_mechanism_answer`；服务未接入则给出基于结构化证据的人工可核查摘要，不宣称已经由工具生成。
