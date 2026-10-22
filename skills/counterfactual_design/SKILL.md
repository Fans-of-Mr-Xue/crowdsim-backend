---
name: counterfactual_design
description: 按事后 API 结构生成可校验的 F-00/CF 反事实实验方案。
when_to_use: 数据版本和关键节点假设已明确，需要设计或修正实验时。
phases: [post]
modes: [plan]
---

# 反事实实验方案模板

需求界定先说明并依次设计三类实验：观察比较平行世界的风险演化；干预检验因素水平与交互效应；机制用隔离、消融或中介对照检验微观到宏观的作用链。每类收集可检验假设、相对 F-00 的变化、控制条件、指标、方法、共同种子与重复次数，以及证据缺口。人工社会的已发布模型与事实基线保持锁定；机制消融仅写入待执行实验配置。

对话任务的 designStage 指向当前类型。experiments 只替换当前类型，experiment_updates 只修改当前类型已有组；纯解释问题不返回变更字段。三类分别由用户确认；公共条件或本类草案发生变化时重新确认。完整方案确认后提交 /api/v1/post/review-plans 保存 reviewIntake 及完整草案，进入方案快照，不自动启动实验。未实测校准评分使用 null。

先用 `get_simulation_context` 读取场景、模型、几何、规则、区域、动作及指标能力；能力未声明时保留草案并标记待映射，不猜造 SUMO 参数。用 `validate_experiment_design` 校验以下完整对象：

```json
{
  "name": "方案名称",
  "datasetId": "已注册的数据集ID",
  "scenario": {
    "scenarioId": "后端声明的场景ID",
    "modelId": "后端声明的模型ID",
    "modelVersion": "后端声明的模型版本",
    "startClock": "20:00",
    "durationSeconds": 15600,
    "geometryVersion": "后端声明的几何版本",
    "ruleVersion": "后端声明的规则版本"
  },
  "seedStart": 1000,
  "parallelism": 1,
  "metricIds": ["从 metricCatalog 选择的指标ID"],
  "pcFeatures": [
    {"id": "feature_1", "metricId": "指标ID", "regionId": "global", "windowStartSeconds": 0, "windowEndSeconds": 600, "aggregation": "peak"},
    {"id": "feature_2", "metricId": "指标ID", "regionId": "global", "windowStartSeconds": 600, "windowEndSeconds": 1200, "aggregation": "mean"}
  ],
  "groups": [
    {"id": "CF-01", "name": "实验组名", "repeatCount": 6, "hypothesis": "可检验假设", "manipulatedFeatureId": "feature_1", "interventions": [
      {"id": "action_1", "actionType": "后端声明的动作", "targetRegionId": "后端声明的区域", "atSeconds": 300, "parameters": {}}
    ]}
  ]
}
```

模板里的版本、动作、区域、指标和数值仅说明字段形状，不可直接作为真实场景参数。F-00 由后端隐式生成；各 CF 组只改变明确的干预变量，按同种子与 F-00 配对。每组写清假设、时点、区域、强度、主要指标、控制项和证伪条件。`pcFeatures` 至少两项，引用的 `metricId` 必须包含在 `metricIds`。后端校验失败时依据字段错误修正，不能跳过校验。

方案确认后可用 `compile_sumo_config` 请求真实适配器编译。若适配器未接入，明确停在“可校验草案”；不得把 API 请求体称作已编译 SUMO 配置。用户确认且后端声明执行能力后，才用 `submit_simulation_task` 启动已创建的实验批次。
