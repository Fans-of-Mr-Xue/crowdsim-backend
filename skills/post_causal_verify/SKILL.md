---
name: post_causal_verify
description: 核验候选因果边是否获得时序和配对干预支持。
when_to_use: 已取得真实 F-00/CF 运行结果和候选图时。
phases: [post]
modes: [plan]
---

# 因果核验

逐条核对 PC 邻接、时序先后、单一干预隔离证据、同种子配对效应及区间。只有后端 `interventionSupportedDirections` 明确支持的方向，才可写“仿真模型内获得干预支持”。其他边保留为未定向候选。记录 `validPairs`、`plannedPairs`、缺失原因、run ID 和模型/规则版本。真实世界因果外推需独立证据，不能由仿真单独推出。
