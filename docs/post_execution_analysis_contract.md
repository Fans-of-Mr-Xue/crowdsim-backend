# 事后 SUMO 执行与分析共同契约

契约标识：`post-execution-analysis/1.0`。核对日期：2026-10-30。本标识用于指引文档，不是 HTTP 响应新增字段。适用于现有纪念塔执行器；其他场景必须另有实际能力声明。

## 1. 实现依据和调用边界

当前生产路径是 `services/application.py → services/execution_template.py → workers/sumo_batch.py → workers/measurement.py / workers/analysis.py / workers/causal.py`，路径均相对 `postanalysis_api/`。`services/metric_calculator.py`、`services/result_service.py` 和 `services/causal_analysis.py` 是另一套未接入该执行器的实现，不能用它们的字段或方向核验结果解释当前输出。

`POST :8767/post-review/chat` 只返回数据核对、目的或设计建议。`historical_event` 可使用后端实际收集的公开资料；其他任务不执行工具。Agent frontmatter 中的工具名不是该接口的可调用能力。上传、保存、编译、启动、结果读取走 `:8765/api/v1/post` 的 HTTP 接口。设计建议、用户确认、保存成功、运行完成分别以实际回执为准。

设计类对话会由 `dialogue_service/post_agent.py` 加载本契约正文；其他场景使用 skill 时，按需读取本文件。HTTP `/capabilities` 与实际校验/执行错误优先于静态描述；能力目录中的通用规则不代表当前执行器已经执行这些规则。

## 2. 当前可执行范围

| 项目 | 实际约束 |
| --- | --- |
| 场景和模型 | `memorial-post-test`、`memorial-post-striping`；当前模型 `post-striping/1.0`、控制 `post-controls/1.0`；几何版本从 capabilities 取得，不手填旧哈希 |
| 数据前提 | 规范数据的 `metadata.sourceRecords` 必须有区域、时间、区间进入/离开，另有 `durationSeconds`；仅人数曲线、普通上传成功或事中报告接入成功，不等于可以执行 |
| 工作台模板 | 从 20:00 起完整重跑 19,800 秒；底层直接实验接口可接受其他时长，但不得超过源数据时间窗，须另行明确设计 |
| 测试映射 | A→M1、B→M2、C→M3；M3 是西侧中部工程假设通道，不是真实北侧通道 |
| 动作 | 仅 C 区域的 `open_gate`、`guidance`；每组每种至多一次，不支持多段控制 |
| 工作台控制量 | `startSeconds` 为 1—19799 整数，同时设置开放与引导启动；`compliance` 为 0—1；`delaySeconds` 为 0—19800 整数 |
| F-00 控制 | 开放/引导启动均 14700 秒，延迟 1500 秒，遵从比例 0.4；这是工程设定，不是测得的历史行为 |
| 重复与资源 | 每组 1—50 次、串行 `parallelism=1`，批次最多 650 次；F-00 重复数为各 CF 重复数最大值，各组使用同一 `seedStart+i` 序列前缀 |

源流量按区间均匀进入、FIFO 重建离开/停留时间；个体停留位置和引导选择使用共同随机数。当前引导在需求编译时按释放时刻、遵从随机数选择出口；开放时刻约束 C 出口人员的释放计划。实际位置由 SUMO striping 产生。不能把它描述为已经模拟了广播传播、现场认知决策或实体闸门的通行能力切换。年龄、国籍只保留边际分布，不产生对应行为差异。

三类方法名固定为：观察 `配对蒙特卡洛模拟`，干预 `一次一因子法（OFAT）`，机制 `机制消融实验`。观察比较完整策略与 F-00；干预相对 F-00 或已确认观察组改变一个控制因素（开放/引导时刻同步平移算一个因素）；机制相对完整观察组只改变遵从比例或响应延迟之一。遵从比例设 0 才称取消遵从模块，其他水平变化称参数敏感性。当前不支持析因交互效应、RCT、中介效应或自然间接效应估计。

## 3. 设计建议、快照与执行请求分层

### 对话建议

`design` 仅允许 `assistant_message` 以及已有的 `parameters/experiments/experiment_updates/group_count/repeat_count`。组字段为 `scheme_type/name/hypothesis/factor/change/method`，局部更新另有已有组 `id`。只有上下文声明 `executionCapabilities` 时才可带 `execution={startSeconds,compliance,delaySeconds,referenceDesignId}`。`factor` 必须在 `availableFactors` 中，方法须在当前目录中。解释问题只返回 `assistant_message`。

`designStage` 限定本轮类型。完整替换必须有一致的 `group_count`；局部更新不能增删组，也不能覆盖其他类型。`repeat_count` 是建议给工作台的每组重复数。不能在组中新增 `primaryMetric/decisionRule/qualityGate/manipulatedFeatureId` 等答复白名单以外的字段；关联特征由工作台现有字段设置。

### 设计充分性要求（skill 要求，不是新增后端硬校验）

每组明确：参照组；具体控制因素和水平；保持不变的条件；主要响应指标及单位；指标覆盖的区域/时窗/人群；预期方向；证伪或判断条件；共同种子和重复依据；数据/机制证据缺口。在已有 `hypothesis/change` 文本及 `assistant_message` 中表达，用户确认后由现有快照保存；缺项先补问。主要终点不得等看到结果再挑选；阈值没有依据时保留待确认，不编造。

比对数值配置，提醒重复组、零变化组和文字/数值不一致。现有 OFAT 校验允许零变化；skill 应要求说明其作为技术重复的用途或建议复用参照。`startSeconds+delaySeconds` 可超过窗口且现有参数校验不拦截；必须说明这意味着窗口内引导可能不生效，只有用户明确设计不生效对照时才如此建议。

### 快照及执行

工作台保存 `executionSettings={seedStart,parallelism,metricIds,pcFeatures}`，组内保存 `execution` 及 `manipulatedFeatureId`，重复数保存于 `counterfactualSettings.repeatCount`。后三者与对话答复是不同层级，不能混写。`manipulatedFeatureId` 只是关联分析特征，不代表执行了 `do(密度)` 等直接变量干预。

用户逐类及完整确认 → `POST /review-plans` → `GET /review-plans/{id}/execution` → 用户核对矩阵 → `POST /review-plans/{id}/execution` → 显式 `POST /experiments/{id}/start`。模板把 OBS/INT/MEC 设计 ID 映射到 CF ID，保留 `sourceGroupId`、`experimentType` 和 `referenceGroupId`；F-00 隐式生成。干预、机制不一定与 F-00 比较，必须使用各组实际参照。`reviewPlanVersion` 与任务内 `reviewSnapshot` 冻结已确认设计。旧快照可能走默认模板，模板数值必须再次核对。

## 4. 测量口径

当前指标版本 `post-metrics/1.1`。积分步长 0.5 秒，FCD 每 5 秒采样；固定 5 米网格与可行人几何裁剪得到有效面积。归档的完整轨迹是完整采样序列，不是每个积分步的位置。

| metricId | 单位 | 当前实现 |
| --- | --- | --- |
| `peak_density` | `person/m2` | 全程所有网格人数/有效面积的最大值 |
| `mean_speed` | `m/s` | 全程人员—时间加权平均速率，包含静止及控制器主动停留人员 |
| `peak_pressure_proxy` | `person/m2*(m/s)^2` | 网格密度乘速度向量方差的时空最大值；不是机械接触力 |
| `risk_exposed_unique` | `person` | 曾进入密度≥3.5网格的去重人数 |
| `risk_exposure` | `person*s` | 密度≥3.5网格人数乘采样时长的累计 |
| `congestion_duration` | `cell*s` | 密度≥3.5且均速≤0.5连续至少10秒的网格时长之和，包含主动停留影响 |
| `evacuation_time` | `s` | 全部计划人员最后一段行走完成的最大到达秒数，从20:00计；未全部到达为 null，并记录右删失 |

这七项是当前执行器的全局全程指标；选中指标不等于支持任意区域/干预后窗口计算。不输出资源成本、四级各自的暴露统计、伤亡或物理接触压力。`evacuation_time` 不是“从干预启动计的疏散耗时”，也没有单独验证真实安全区。

PC 特征仅使用已选的密度、速度或压力代理指标，2—8项，区域 `global/A/B/C`，整数窗口满足 `0≤start<end≤19800`（左闭右开），汇总 `peak/mean/end`。它对区域采样序列取最大值、有效帧算术均值或最后有效帧；速度特征的 `mean` 与全程人员—时间加权速度不同。缺失帧不填零。区域密度/压力取与该入口相交网格的极值，速度按入口 edge 上人员计算；不能当作任意严格区域裁剪后的统一统计。

## 5. 分析结果实际字段

`GET /results/{experimentId}/{section}` 返回外层 `experimentId/resultId/section/definitionVersion/data`。section 的 `causal-graph` 映射存储字段 `causalGraph`。当前分析版本 `post-analysis/1.0`，结果文档另有 `metricDefinitionVersion`；不要把外层分析版本当指标版本。

| section | `data` 的当前结构 |
| --- | --- |
| `metrics` | `distributions[]`：`groupId/metricId/n/mean/std/unit`；`runs[]`：运行摘要及 measurement/calibration/executionEvidence |
| `comparison` | `effects[]` 和 `method` |
| `observation`、`mechanism` | `experiments[]`、`status`（有组为 computed，无组为 not_designed） |
| `intervention` | 同上，另有 `causalGraph` |
| `causal-graph` | PC 候选图及状态；不含 `interventionSupportedDirections` |

每组结果含 `groupId/name/hypothesis/referenceGroupId/interventions/effects`。每项 effect 使用 `referenceGroupId/groupId/metricId/unit/pairCount/seeds/estimate/ci95/direction/method/inferenceStatus`。`estimate=候选组−参照组`，只取成功运行中同种子且指标有效的交集；至少2对才给2000次配对 Bootstrap 区间，少于20对标记 `descriptive_only`。20对以上仍不保证功效或区间精度。`n` 是单组该指标有效样本数，`pairCount` 是该比较的有效配对数，不能互换。

当前不返回 `validPairs/plannedPairs/missingReason` 等旧协议字段；计划数从任务配置读取，缺失/失败/删失原因结合运行详情和 measurement 核对。派生展示说明必须标明来源，不能伪称后端返回。`computed` 仅表示进行了计算，不能解读为假设成立、机制链已证实或所有指标有效。

PC 只使用成功的独立 F-00 种子，每次完整运行形成一行；至少 `max(20,特征数+4)` 个完整数值样本，常量列返回 `degenerate_data`，样本不足返回 `insufficient_data`；奇异检验可返回 `partially_estimable`。算法为 PC-Stable 骨架加未屏蔽碰撞结构、Fisher-Z、α=0.05，不做 Meek 补全。方向只标记 `collider` 或 `undirected`，均属候选结构。当前没有干预支持方向核验器；即使效应区间不跨零，也不能自动给 PC 边定向。

机制 section 是相对完整观察组的配对消融/敏感性效应。运行详情的 `controlEvents` 主要为 hold/release/release_before_hold；`assignedExitCounts` 是编译分配，`actualEventCounts` 是实际控制事件计数，均不是实测出口到达计数。可以分析消融效应，缺少接收/遵从/改道/到达链时，作用路径保持未验证。

## 6. 有效性与保存读取边界

运行前核对源流量、测量区域和目标人群能否对应；运行后检查人数校准残差、到达/计划人数、极端密度、主动停留和指标在各组的区分能力。持续堵塞、全删失或恒定特征时，说明具体诊断项，再决定正式重复数。零效应也是可报告结果，不能用“干预必须改善”作为质量门槛。

数据集、快照、任务、runs、results 位于 `postanalysis_*`；`post` 索引连接每次运行的明细/轨迹/结果集合。上传原件及编译 manifest/routes 位于 GridFS；轨迹按 sequence 分块，通过 `/runs/{id}/trajectory` 分页；实际事件与校准明细通过 `/runs/{id}` 读取。原始 FCD/tripinfo/日志仍在本机输出目录，未提供统一文件下载 API。报告中分别写明可经 API 取回的证据和仅有文件路径的产物。

读取保留 owner/workspace，按 datasetId/version、reviewPlanVersion、experimentId、planId、sourceGroupId、referenceGroupId、seed、runId、resultId 对齐。结果 API 接受 completed/partial 且有 resultId 的任务；当前真实执行器遇错/取消会停止，尚不产生部分分析封存，也不支持恢复补跑。重启仅标记 interrupted。不得承诺失败批次可自动继续分析、幂等请求能修复全部中间写入或存档已具备完整独立复现能力。

需要扩展时参照 [SUMO 与后端对接清单](post_sumo_handoff.md)。该清单中的拟新增字段、算法和恢复机制均不属于当前能力。
