# 人群 Agent 建模与论文依据

参数取值的证据分级、逐项数据要求、文献先验和外滩标定流程，见[《人群 Agent 参数设定依据与校准方案》](./人群Agent参数设定依据与校准方案.md)。

## 1. 模型边界

本系统研究道路空间中的人群积聚、排队、低速、拥堵、改道和拥堵转移。实际二维位置与局部避碰由 SUMO 1.24.0 striping 计算；Python 不实现第二套坐标推进器，也不把心理压力解释为身体接触压力。

SUMO 官方说明 striping 在 sidewalk、walkingarea 和 crossing 内维护二维坐标，以条带实现避碰，并能在流入超过流出时形成排队或堵塞；其 jamtime 机制会在长期阻塞后解堵。因此实验固定并记录 jamtime 参数。[SUMO Pedestrians](https://sumo.dlr.de/docs/Simulation/Pedestrians.html)

Helbing–Molnár 社会力模型说明了期望速度、人与人/边界排斥及吸引项的经典连续动力学表达，但本工程没有实现社会力方程，不能引用该论文声称当前运动引擎是社会力模型。[Helbing & Molnár, 1995](https://doi.org/10.1103/PhysRevE.51.4282)

## 2. 系统循环

1. `SimulationRuntime` 在时间边界冻结 SUMO `MotionSnapshot` 和上一时刻 `AgentState`。
2. `InformationModel` 处理到期消息；`CrowdEnvironment` 仅从个人半径内、同一可步行拓扑上的真实 person 生成 `Observation`。
3. `DecisionScheduler` 收集到期对象。规则模式处理全部对象；LLM 模式受预算和并发限制，未获得额度者明确规则回退。
4. 规则与 LLM 都输出 `BehaviorPlan`；`PlanExecutor` 校验 person、snapshot、动作和候选道路后通过 TraCI 生效。
5. SUMO 唯一推进一个 0.5 s 步长；Python不插值、不随机重定位、不补人。
6. `StateUpdater` 从冻结旧状态批量计算压力、疲劳、风险和受阻时间；`PopulationManager` 对账出发、在场、正常到达和显式移除。
7. `MetricsCollector` 从真实快照和明确 lane 面积计算人数、速度、密度、队列和边转换，`FrameSerializer` 输出同一新时间边界。

## 3. Agent 类型和关系

一个 SUMO person 就是一个行人，统计权重恒为 1。行为差异通过组合数据而不是派生大量代码类表达：

- `AgentProfile`：稳定异质属性；
- `AgentState`：压力、疲劳、已知信息、目标和当前计划；
- `MotionSnapshot`：SUMO 权威运动状态；
- `GroupRecord`：真实成员 ID、领队和会合点；
- `InformationMessage`：来源、产生/送达/失效时间和范围。

Moussaïd 等对自然条件下约 1500 个行人群组的研究支持“同行社会互动会影响群组行走和整体流动”这一机制方向；本工程据此保留显式同行关系、有限等待和会合候选，但没有实现或宣称连续 V 形队形已被复现。[Moussaïd et al., 2010](https://doi.org/10.1371/journal.pone.0010047)

## 4. 最终活动属性

所有活动属性都有唯一消费者，分布见 `config/population_profiles.json`，完整机器可读登记见 `config/attribute_registry.json`。

| 属性 | 用途 | 参数来源 |
|---|---|---|
| `free_walking_speed` | 基础速度上限 | 工程假设，待轨迹标定 |
| `mobility` | 缩放个人能力速度 | 工程假设 |
| `perception_radius` | 局部邻域半径 | 工程假设 |
| `familiarity` | 路线成本/陌生度评分 | 工程假设 |
| `risk_tolerance` | 已知风险触发改道阈值 | 工程假设 |
| `patience` | 持续受阻后的等待策略 | 工程假设 |
| `crowding_tolerance` | 客观密度到主观拥挤的映射 | 工程假设 |
| `following_tendency` | 同行协调候选 | 工程假设 |
| `authority_compliance` | 官方信息采信修正 | 工程假设 |
| `information_trust` | 按消息来源计算采信 | 工程假设 |
| `group_cohesion` | 同行等待/跟随候选 | 工程假设 |
| `stress_susceptibility` | 压力刺激率 | 工程假设 |
| `recovery_seconds` | 压力恢复时间常数 | 工程假设 |
| `endurance` | 疲劳增长与休息恢复 | 工程假设 |

这些数值不是论文测得的外滩参数。论文只支撑机制方向；没有现场数据拟合前，结果只能称为机制实验。

## 5. 背景属性与文化边界

`age_group`、`occupation`、`nationality`、`native_language`、`visit_purpose` 被保留。`visit_purpose` 可初始化活动候选，`age_group` 可用于未来有数据依据的条件分布；occupation、nationality、native_language 当前只展示，不进入有效规则输入或 LLM prompt。

系统不使用“某国籍固定更服从、更从众或走得更快”的文化查表。所有收到消息默认能够理解，但仍区分收到、采信和行动。旧字段 `language_delay`、`symbol_accuracy`、`same_culture_attraction` 等列入 registry 的 deprecated 区，模型与指标不读取。

## 6. 环境、密度和突发事件

客观局部密度定义为：

```text
同一拓扑观察区域内实际SUMO person数 / (2 × perception_radius × 可步行lane宽度之和)
```

面积无法可信获得时返回 unknown，不伪造默认密度。事件描述、压力、传言和视觉颜色都不能改变客观人数或面积。事件只有两条作用链：

- 信息刺激：alarm、rumor、police_guidance、temporary_diversion 经消息送达、信任和决策影响后续行为；
- 物理危险输入：静态范围与强度产生明确个人速度上限，清除后重新计算组合上限。

积水只是一般 `HazardZone` 的一个类型；警察引导不会降低水位、增加道路宽度或修改容量。动态多边形硬障碍和硬封路未实现时明确拒绝。

## 7. 策略执行

`continue`、`slow_down`、`wait`、`reroute`、`change_goal` 都由 `PlanExecutor` 执行。有效速度为个人能力、策略和危险上限的最小值。wait 是当前速度上限 0，不误用“向计划末尾追加等待”；活动停留则预先追加 waiting stage 和后续 walking stage，避免活动到达后 person 被删除。

reroute/change_goal 只能使用 `RouteProvider` 给出的已加载可步行道路；执行时替换真实 SUMO walking stage，不调用 `moveToXY`。`follow_group` 和 `seek_help` 只是意图，必须先归约成可执行等待或路线计划。

PedSUMO 提供了“外部决策经 TraCI 读写 SUMO 行人状态”的相关架构先例，但不证明本项目的心理、拥堵或参数已经验证。[PedSUMO, HRI 2024](https://doi.org/10.1145/3610977.3637478)

## 8. 机制—代码—来源—验证

| 机制 | 工程位置 | 来源性质 | 验证 |
|---|---|---|---|
| 二维行走、避碰、walkingarea | `crowdsim/infrastructure/sumo_adapter.py`、SUMO striping | SUMO 官方实现 | F01、F08、F14 |
| 一人一实体和生命周期 | `crowdsim/core/population_manager.py` | 工程守恒合同 | F02 |
| 可复现异质画像 | `crowdsim/domain/population_profiles.py` | 未标定工程分布 | F03 |
| 局部观察与真实密度 | `crowdsim/environment/crowd_environment.py` | 明确定义的操作性指标 | F04 |
| 压力恢复与疲劳 | `crowdsim/core/state_updater.py` | 工程状态方程 | F05 |
| 消息送达、信任、失效 | `crowdsim/environment/information_model.py` | 工程机制假设 | F06 |
| 活动停留和离场 | `crowdsim/environment/poi_catalog.py`、`crowdsim/decision/plan_executor.py` | SUMO stage 合同 | F07 |
| 同行协调 | `crowdsim/domain/group_manager.py` | Moussaïd 等机制方向 | F09 |
| 规则/LLM共享计划 | `crowdsim/decision/agent_decision.py`、`crowdsim/decision/decision_scheduler.py` | 工程对照设计 | F10、F11、F17 |
| 危险和引导分离 | `crowdsim/environment/hazard_model.py`、`crowdsim/environment/intervention_executor.py` | 工程因果边界 | F12、F13 |
| 指标与回放 | `crowdsim/infrastructure/metrics.py`、`crowdsim/infrastructure/experiment_recorder.py` | 可复现实验要求 | F14、F16、F18 |

## 9. 结论限制

- striping 能产生拥堵不表示当前场景参数已经拟合真人。
- LLM 的解释文字不是验证证据，也不预设 LLM 必须优于规则。
- 社会力和群体论文不能用来声称当前系统已实现身体接触、踩踏压力或连续二维群组队形。
- 真实性需要现场流量、速度、轨迹、宽度和行为数据校准，并用多随机种子报告不确定性。
