# 事中阶段 1：预设接入实际实验流程

2026-10-14 完成代码接入及实际验收。累计 6 次完整真实运行覆盖默认五个种子与额外同种子复现；逐步判定、暂停恢复、故障排除、归档报告和服务重启读取均通过。低电量中断留下的取消记录已保留，缺失复现轮通过新实验补齐。详见 [验收记录](during_stage1_acceptance.md) 和 [机器可读证据](validation/during_stage1.json)。

## 范围与入口

复用现有 `crowdsim.experiments` 实验服务和 Overlay WebSocket 后端。新增预设 ID 的解析、配置快照、每轮初始化和每步事件统计；本阶段仅开放 C0 无外部控制基线。未修改聊天页、方案页或其原有虚拟实验逻辑，页面接入属于下一阶段。

纪念塔预设仍固定为 700 人、最长 1800 个仿真秒、步长 0.5 秒。核心区域是 H1、H2a、H2b，即纪念塔外环的三个 SUMO 路段。橙色与红色人数合计不少于 10 人、占核心区人数不少于 20%，连续至少 10 秒确认事件；正式观测从第 1 秒开始。

实验服务提供以下接口，沿用现有的用户和工作区作用域、创建/启动分离及响应封装：

| 接口 | 用途 |
|---|---|
| `GET /crowdSim/control/during-presets` | 返回当前可用预设、完整需求、规则和配置指纹；只读，不启动实验 |
| `POST /crowdSim/control/experiments` | 根据预设创建并保存实验配置 |
| `POST /crowdSim/control/experiments/{experiment_id}/start` | 将该实验的各个种子轮次加入既有运行队列 |
| `GET /crowdSim/control/experiments/{experiment_id}` | 查询实验及各轮状态 |
| `GET /crowdSim/control/experiments/{experiment_id}/report` | 生成/查询实际记录汇总的报告 |

服务也接受现有的 `/api/` 前缀。最小创建请求如下：

```json
{
  "name": "纪念塔持续拥挤基线",
  "scenario": {
    "presetId": "memorial-tower-during-v1"
  }
}
```

默认使用 C0、1800 秒观察窗口、30 秒实验服务观测间隔和 20260908–20260912 五个种子。可显式提供 `scenario.seedSet`（1–30 个不重复的非负整数）；人数、地点和时长必须与预设一致，规则由服务端预设决定。修改人口、叠加突发事件、使用 C2 等控制方法会明确拒绝，避免生成名为基线但行为不同的实验。后续策略支持将单独扩展。

## 每轮执行和可追溯性

创建实验时，将预设 ID、版本、SHA-256 指纹、资源校验和、完整需求模板及事件定义绑定到 `scenario.duringPreset`。开始排队、每轮准备以及运行时初始化都验证预设/资源，资源变更不能无提示沿用旧结果。

每轮执行顺序：

1. 提交预设的完整需求，获得独立 `requirement_id` 并核验需求指纹；不读取或借用浏览器当前需求。
2. 通过带预设 ID、版本、指纹、人数和种子的 `reset` 创建新的规则模式运行时。
3. 显式配置 700 人和 1800 秒终点，同一随机种子用于需求生成、画像采样和 SUMO。显式种子替换旧 `--seed`，并关闭 SUMO 的随机种子模式。
4. 校验初始化人数、绑定的预设指纹及返回的实际运行 ID，再开始推进。
5. 后端每个 0.5 秒仿真步更新持续拥挤计时、行程完成率、首次达到 95% 的时刻及峰值。实验服务按较低频率接收并保存累计结果，结束帧强制保存。

实验服务的 `run_id` 和 SUMO 的 `runtime_run_id` 分别记录；后者对应本地 `runs/<runtime_run_id>/` 目录。处理帧时校验运行 ID，避免上一轮的结束帧结束新轮。每次重置都会清空事件累计器、峰值和完成时间。

WebSocket 预设重置字段为：`requirement_id`、`count`、`seed`、`during_preset_id`、`during_preset_version`、`during_preset_fingerprint`。这些由实验服务组装。未携带预设 ID 的普通实验仍走原有配置路径。

## 结果与归档

初始化帧和更新帧通过 `metrics.during` 提供后端证据；实验观测保留为 `during`，不根据渲染行人数重新计算。

| 字段 | 含义 |
|---|---|
| `preset_id` / `preset_version` / `preset_fingerprint` | 该轮绑定的预设身份 |
| `sample` | 当前核心人数、橙红人数、未知数、核心平均密度、全场受阻和限流暂缓人数 |
| `phenomenon` | 发生标志、覆盖情况、首次确认时间、持续时长、峰值及各个事件片段 |
| `population` / `population_valid` | 完整人口账本及有效性检查 |
| `completion_rate` | 正常完成行程人数 / 计划人数 |
| `t95_from_start_seconds` | 从仿真起点算起的精确跨越时刻；未达到为 `null` |
| `peaks` | 每步记录的核心人数/密度、全场受阻和控制暂缓峰值 |
| `finished` / `end_reason` / `run_complete` | 运行结束及完整性；1800 秒到期或全体正常完成可形成完整结果 |

缺失正式观测会中断连续确认，不能当作没有拥挤。初始化 0.5 秒帧保留但排除判定。未结束的已确认事件在实时快照中标记 `ongoing`；正常结束时标记 `observation_end` 和截断状态，已结束的片段保留其实际结束原因。

正常结束不要求达到 95% 完成率。到 1800 秒仍有未完成者可以得到有效的完整观察结果，完成时间保持 `null`。人口不匹配、不守恒、未知消失或显式移除会使预设基线无效；提前结束且没有全部完成也不能报告运行成功。失败后尝试暂停对应运行，失败/取消状态不进入有效频率分母。

每个 SUMO 运行目录新增：

- `during_preset.json`：完整预设快照，另有既有 `requirement.json`。
- `during_observations.jsonl`：逐步观测与累计判定。
- `during_result.json`：正常到达终点时的最终结果；其 `run_complete` 决定是否完整。
- `manifest.json`：预设身份、指纹及上述文件路径。

实验服务仍使用既有仓库保存实验、运行、观测和指标。报告每轮保留 `during`，并提供 `crowdingOccurred`、`crowdingCoverage`、`firstCrowdingConfirmedAtSeconds`、`crowdingDurationSeconds`、`peakCrowdedCount`、`peakHighRiskCount`、`peakCoreDensity`、`peakBlockedLowSpeedCount`、`peakControlHeldCount`、`finalCompletionRate`、`t95Seconds` 等摘要字段。完成率和 95% 时间直接采用逐步累计值，不用 30 秒样本插值或补零。

报告顶层 `during.occurrenceFrequency` 的分母只含状态为 completed、人口有效、事件覆盖完整且预设一致的轮次。失败、取消、观测不完整及待运行轮次分别计数，无有效轮时频率为 `null`。即使不完整轮已有确定发生片段，也保留事实但不加入完整观察窗口的频率统计。这是预设仿真的样本频率。

## 代码级检查

已对判定、预设契约、替身网关下的执行流程、结束帧、重置隔离、归档、存储重载和报告汇总进行隔离单元测试，共 90 项通过（含并发启动回归测试）。15 个 Python 文件通过语法/编译检查，预设资源指纹和差异格式检查通过。检查过程中额外拦截了 SUMO 启动、外部进程、网络连接和监听操作。

相关测试文件为 `tests/test_during_crowding.py`、`tests/test_during_experiments.py`、`tests/test_control_experiments.py`。真实仿真已完成本阶段验收；启动矩阵的检查与创建已加互斥保护，实际并发请求复验通过。后续可接入聊天和方案页。
