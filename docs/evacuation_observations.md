# B1/B2/B3 疏散观测（简化口径，schema_version=2）

这些指标只记录已有仿真，不改变行人路线、热点活动或心理模型，不生成分析报告。
历史需求无需重新保存；加载时刷新指标能力声明。仅计算需求中所选指标及其必要依赖。

## 时点与完成条件

- `t0`：该 run 首次成功开始运行的后端仿真时间。初始化可能已到 0.5 秒，使用实际时间。
- `ta`：后端首次成功应用 `police_guidance` 或 `temporary_diversion` 的时间；仅排队或前端保存不算应用。`observe_only` 只记录采用，不启动疏散计时。
- `te`：首次满足本轮目标行人全部出现在 SUMO 正常 arrived 台账、全网无活动行人、无待出发目标的时间。不能有显式删除、未知消失、人数守恒错误或新增的非本轮目标行人。

目标编号在 `t0` 固定为计划行人与已出发行人的并集，记录在 `evacuation_state.json`。
所有时间均为 SUMO 秒，暂停、页面往返和决策生成所用的现实时间不计入。重连同一
run 会保留计时；后续策略只追加历史，不覆盖首次策略的时间和密度。显式重置产生新 run。

**正常完成行程不证明行人越过了所选区域边界或到达真实安全区。** 现有策略是信息干预，
记录中的 `physical_change=false` 保留这一事实；自然离场也可能促成最终结果，不能据此
将人群消减全部归因于策略效果。

## 指标定义

| 指标 | 计算 | 单位 |
| --- | --- | --- |
| B1 疏散时间 | `te - ta` | 仿真秒 |
| B2 疏散效率 | `(ρa - ρe) / (te - ta)`，密度按所选区域面积 | 人/(平方米·仿真秒) |
| B3 人群密度差 | 同一固定均匀片区的 `abs(ρ0 - ρt)`、`abs(ρt - ρe)`、`abs(ρ0 - ρe)` | 人/平方米 |

另记录总事件时间 `te - t0` 和尚未完成时的已用时间 `elapsed_since_strategy_seconds`。
B3 是按片区和时间的分布序列，标量 `value=null`，不是只比较全区域平均密度。片区为
整轮固定的默认 20 米网格，边缘面积裁剪到所选多边形；不支持随机片区。

## 文件与有效性

每轮目录 `runs/<run_id>/`：

- `observation_result.json`：各指标状态、最终 B1/B2 数值、时点及进度。文件最外层
  `status=complete` 表示观测记录结束；判断疏散完成须读取 `evacuation.status` 和具体指标状态。
- `evacuation_state.json`：初始、首次策略、完成时的密度分布，目标编号及各次成功策略记录。
  首次运行和策略应用立即落盘，即使当时暂停、尚未发生下一步。只选 B1 时不额外计算密度网格。
- `evacuation_progress.jsonl`：逐快照进度；读取时按 `observation_samples.jsonl` 排除未完整提交的快照。
- `observation_cells.csv`：B3 所需的运行时分布及 `absolute_initial_difference_person_per_m2`。
  首次运行前此差值为空；无法定位的样本 `density_valid=false`，相应差值为空。
- `evacuation_density_differences.csv`：只有正常完成且完成快照落盘后才生成。按完整快照索引
  从原始片区序列生成三组差值，覆盖 `t0` 到 `te`。初始/结束差会在各时间行中重复，便于比较。

实时帧的 `metrics.observations.evacuation` 提供同样的简要进度和数值。
`observation_geometry.json` 和 `manifest.json` 保存口径、单位、几何及文件引用。

| 状态 | 含义 |
| --- | --- |
| `not_started` | 未首次运行，或 B1/B2 尚无有效策略 |
| `in_progress` | 已启动，继续记录，最终值为空 |
| `complete` | 满足正常完成条件，对应指标可用 |
| `incomplete` | 达到时限或引擎结束，但未满足正常完成条件 |
| `interrupted` / `error` | 提前关闭或异常，保留已有证据，最终值为空 |
| `invalid_population_accounting` | 删除、未知消失、意外新行人或人数不守恒，不能认定正常完成 |
| `invalid_density_baseline` | 密度基线存在无法定位的行人，B2/B3 不伪造数值；B1 可独立有效 |
| `zero_duration` | B2 分母为零，数值为空 |
| `no_target_population` | 没有目标行人，不视为成功疏散 |
| `uncommitted_completion` | 观察到完成，但完成快照写入失败，最终值不作为已记录结果发布 |

未完成的运行保留初始/运行时的密度差，不构造“疏散后”分布，也不以时限代替 B1。
没有有效策略时，B3 仍可在自然行程完成后形成完整序列；B1/B2 保持 `not_started`。
