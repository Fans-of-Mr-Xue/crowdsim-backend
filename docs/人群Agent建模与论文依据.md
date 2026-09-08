# 人群 Agent 建模与论文依据

## 1. 文档目的

本文档说明 CrowdSim 后端中人群 Agent 的建模范围、数据结构、交互机制、关系模型和论文依据，并明确区分：

1. 已由论文或实证研究支持的模型机制；
2. 已在代码中实现并参与计算的属性；
3. 已保留并提供给大模型、但尚未进入本地行为方程的属性；
4. 来自项目研究方案、仍需实验或调查标定的参数。

本项目的核心目标是构建以下闭环：

```text
环境与人群状态
→ Agent 局部感知
→ 个体心理和认知状态变化
→ 本地规则或大模型生成策略
→ 运动引擎执行行为
→ Agent 位置和人群分布变化
→ 形成下一轮环境
```

文化标签不直接决定行为。文化画像只提供参数分布，Agent 在该分布内采样并保留个体差异。

---

## 2. Agent 类型设计

建议采用“基础类型 + 社会身份 + 群体角色 + 心理类型”的组合，而不是为每种文化创建独立代码类。

| 维度 | 类型 | 作用 | 当前状态 |
|---|---|---|---|
| 基础类型 | `pedestrian` | 行人感知、决策和移动 | 已实现 |
| 基础类型 | `vehicle` | 道路车辆流动 | 已实现，但不是本文重点 |
| 社会状态 | `individual` | 独立行动者 | 待显式建模 |
| 社会状态 | `group_member` | 家庭、朋友或旅游团成员 | 当前仅有 `group_size` |
| 群体角色 | `leader` | 决定群体目标和路线 | 待实现 |
| 群体角色 | `follower` | 跟随领队并协调速度 | 待实现 |
| 心理类型 | `stable` | 情绪感染敏感度较低 | 可由属性组合表达 |
| 心理类型 | `sensitive` | 情绪感染敏感度较高 | 可由属性组合表达 |
| 行动能力 | `normal` | 普通行动能力 | 已由 `mobility` 表达 |
| 行动能力 | `limited_mobility` | 老年或行动不便 | 待建立专用参数分布 |

Moussaïd 等通过约 1500 个自然行人群体的观察发现，城市行人中大量个体以情侣、朋友和家庭等社会群体移动，群体队形会显著影响流动。因此，独行者和群体成员必须区分。[Moussaïd et al., 2010](https://doi.org/10.1371/journal.pone.0010047)

Leader–follower 模型表明，领队与跟随者角色能够表达排队、集体移动和临时跟随关系。[Leader–follower model](https://www.sciencedirect.com/science/article/pii/S0925753515003136)

---

## 3. Agent 属性

### 3.1 身份与运动状态

| 属性 | 含义 | 当前用途 |
|---|---|---|
| `id` | Agent 唯一标识 | 查找和前端展示 |
| `kind` | `pedestrian` 或 `vehicle` | 选择运动规则 |
| `route` | 路段序列 | 导航和移动 |
| `route_index` | 当前路段索引 | 定位当前路段 |
| `distance` | 当前路段行进距离 | 计算空间位置 |
| `lateral` | 相对路段中心线横向偏移 | 地图展示和位置区分 |
| `base_speed` | 自由流期望速度 | 速度计算基线 |
| `speed` | 实时速度 | 每步移动距离 |
| `mobility` | 行动能力系数 | 直接修正实际速度 |

文化画像中的 `desired_speed` 在创建 Agent 时映射为 `base_speed`，没有丢失：

```text
base_speed = 期望/自由流速度
speed = 环境、心理状态和决策影响后的实时速度
```

### 3.2 文化与个体参数

| 属性 | 含义 | 是否进入本地计算 | 是否进入大模型上下文 |
|---|---|---:|---:|
| `cultural_group` | 文化画像标签 | 间接，通过画像生成参数 | 是 |
| `native_language` | 母语 | 否 | 是 |
| `personal_space` | 偏好个人空间 | 待实现二维排斥力 | 是 |
| `density_tolerance` | 可容忍密度上限 | 是 | 是 |
| `barrier_compliance` | 遵守隔离设施概率 | 待实现 | 是 |
| `queue_mode` | 排队方式 | 待实现 | 是 |
| `shortest_path_weight` | 最短路径偏好 | 待接入路径代价 | 是 |
| `low_density_path_weight` | 低密度路径偏好 | 待接入路径代价 | 是 |
| `following_tendency` | 跟随人群倾向 | 待实现方向跟随 | 是 |
| `language_delay` | 非母语信息处理延迟 | 待实现信息模型 | 是 |
| `symbol_accuracy` | 标识理解准确率 | 待实现信息模型 | 是 |
| `authority_compliance` | 权威指令服从概率 | 待实现指令模型 | 是 |
| `information_trust` | 不同信息源信任度 | 待实现信息模型 | 是 |
| `same_culture_attraction` | 同文化聚集倾向 | 待实现关系吸引 | 是 |
| `group_cohesion` | 家庭或群体凝聚力 | 待建立显式群体 | 是 |
| `help_seeking` | 求助倾向 | 待实现 | 是 |
| `conflict_threshold` | 冲突触发阈值 | 待实现 | 是 |
| `panic_susceptibility` | 压力/恐慌感染敏感性 | 是 | 是 |
| `stress_response` | 应激行为偏好 | 部分参与决策 | 是 |
| `counterflow_tendency` | 逆向流倾向 | 待实现关系寻回 | 是 |
| `recovery_seconds` | 压力恢复时间 | 待替换固定恢复系数 | 是 |

### 3.3 群体与感知属性

| 属性 | 含义 | 当前状态 |
|---|---|---|
| `group_size` | 所属群体规模 | 已参与局部人数统计 |
| `perception_radius` | 局部感知半径 | 已参与邻居识别 |
| `acceptable_people` | 感知范围内可接受人数 | 已参与密度分级 |
| `nearby_people` | 当前感知到的人数 | 动态更新 |
| `local_density` | 当前局部密度 | 动态更新 |
| `density_level` | `free/busy/crowded/critical` | 动态更新 |

当前 `group_size` 只是数值，尚不能表示“另外几个成员是谁”。后续必须增加 `group_id`、`leader_id` 和成员列表。

### 3.4 心理、事件与决策状态

| 属性 | 含义 | 当前用途 |
|---|---|---|
| `stress` | 个体压力 | 决定等待、减速或避让 |
| `fatigue` | 疲劳程度 | 降低速度 |
| `flood_impact` | 旧积水兼容影响 | 速度和规避 |
| `event_impact` | 通用事件暴露 | 压力和决策 |
| `event_avoidance` | 事件规避压力 | 避让和绕行 |
| `event_phase` | `gathering/emergency` | 决策上下文 |
| `decision` | 当前标准化动作 | 控制行为执行 |
| `decision_reason` | 决策原因 | 可解释性和调试 |
| `decision_source` | `local/llm/local_fallback` | 标识决策来源 |
| `next_decision_step` | 下次决策时间 | 控制调用频率 |

---

## 4. 交互模式

### 4.1 Agent—环境交互

```text
路段密度、拥堵、事件、积水与管理影响
→ Agent 感知人数、密度和事件压力
→ 更新压力、疲劳与密度等级
→ 生成 continue/slow_down/avoid/follow_crowd/wait
→ 改变速度、停留和路径
```

该闭环已经实现。决策在状态更新后生成，主要作用于下一仿真步，形成一个时间步的感知—判断—执行延迟。

### 4.2 Agent—Agent 物理交互

计划采用以下结构：

```text
目标驱动力
+ 个人空间排斥
+ 碰撞规避
+ 身体接触
+ 高密度摩擦
+ 群体吸引
```

Helbing 等提出的社会力模型使用目标驱动力、人与人排斥、墙体排斥、身体接触和摩擦解释拥堵、堵塞和高密度行为，可作为物理交互依据。[Helbing et al., 2000](https://doi.org/10.1038/35035023)

Moussaïd 等提出基于视觉可行方向和障碍距离调整方向与速度，适合替代单纯按人数减速的规则。[Moussaïd et al., 2011](https://doi.org/10.1073/pnas.1016507108)

当前系统尚未实现完整二维接触力，只实现同一路段局部人数和压力感知。

### 4.3 情绪和压力交互

当前压力更新结构为：

```text
stress_i(t+1) =
    历史压力
  + 邻近人数压力
  + 局部密度压力
  + 事件刺激
  + 邻居压力 × panic_susceptibility_i
  - risk_tolerance_i 与 familiarity_i 提供的恢复作用
```

动态危险源、异质人格、情绪传播和运动耦合可以参考 Cao 等的 OCEAN—情绪感染—社会力组合模型。[Cao et al., 2023](https://doi.org/10.1016/j.ijdrr.2023.103902)

情绪感染模型普遍存在标定数据不足的问题，因此 `panic_susceptibility` 等数值应视为待标定参数，而不是通用经验常数。[情绪感染系统综述](https://doi.org/10.1007/s10458-022-09589-z)

---

## 5. Agent 关系模型

当前系统尚未建立显式关系，只保存了 `group_size` 和群体倾向参数。目标结构如下：

```python
AgentGroup:
    id
    group_type          # family/friends/tour_group
    leader_id
    member_ids
    cohesion
    formation
    preferred_distance
    regroup_distance

AgentRelation:
    source_id
    target_id
    relation_type       # family/friend/leader_follower/stranger/same_culture
    tie_strength
    trust
    preferred_distance
    information_influence
    emotion_influence
```

| 关系 | 预期作用 | 论文依据 |
|---|---|---|
| `family` | 强凝聚、等待、重组 | Ivo 2021 |
| `friend` | 并排行走、速度协调 | Moussaïd 2010 |
| `tour_group` | 共同路线、领队跟随 | Leader–follower 模型 |
| `leader_follower` | 方向和信息不对称 | Leader–follower 模型 |
| `stranger` | 主要发生物理排斥 | Helbing 2000 |
| `same_culture` | 弱吸引或信任修正 | 当前为研究假设，需标定 |

Qiu 和 Hu 的模型显式考虑群体规模、队形、内部结构、成员影响强度和 leader–follower 结构，适合作为基础群体关系模型。[Qiu & Hu, 2010](https://doi.org/10.1016/j.simpat.2009.10.005)

Ivo 等支持任意规模群体、子群体、单领队或层级领队、不同关系强度、松散/紧密凝聚以及掉队后重组，适合指导 `AgentGroup + AgentRelation` 的实现。[Ivo et al., 2021](https://doi.org/10.1016/j.cag.2021.08.005)

疏散或行动决策的社会传播可以采用加权阈值：

```text
已行动关系邻居的权重之和 > Agent 决策阈值
→ Agent 改变决策
```

该机制可以参考社会传染阈值模型，其研究了关系强度、社群结构、初始信息源和观察时间窗口对集体决策传播的影响。[Threshold social contagion model](https://doi.org/10.1016/j.trb.2011.07.008)

---

## 6. 文化属性的建模原则

### 6.1 保留全部文化属性

现有文化、信息、社会和应激属性不会因为尚未进入运动公式而删除。它们有两个用途：

1. 由本地机制逐步实现明确、可验证的行为作用；
2. 作为大模型分析 Agent 策略的完整上下文。

### 6.2 文化标签不等于确定行为

正确形式：

```text
文化画像 → 参数概率分布 → 个体采样 → 现场数据校准
```

不应采用：

```text
某文化群体 → 固定服从、固定从众、固定速度
```

### 6.3 当前文化分布

项目研究方案给出的初始组成是：

| 画像 | 比例 |
|---|---:|
| 东亚 | 70% |
| 欧美 | 20% |
| 东南亚 | 5% |
| 其他集体主义画像 | 5% |

这些比例是场景配置，不是普适人口规律。

### 6.4 跨文化个人空间依据

Sorokowska 等比较了 42 个国家的偏好人际距离，可用于初始化 `personal_space` 的文化修正项。但该研究测量偏好距离，不等同于动态高密度人群中的身体距离，因此不能直接作为碰撞半径。[Sorokowska et al., 2017](https://hraf.yale.edu/ehc/documents/1132)

建议形式：

```text
personal_space_i =
    基础个人空间
  × 文化修正
  × 个体随机扰动
  × 密度压缩系数
```

---

## 7. 大模型决策接口

大模型应接收完整但分层的 Agent 状态：

```text
physical_profile
cultural_profile
social_profile
cognitive_state
emotional_state
perception
environment
```

示例：

```json
{
  "profile": {
    "cultural_group": "east_asian",
    "personal_space": 0.58,
    "density_tolerance": 7.2,
    "following_tendency": 0.86,
    "authority_compliance": 0.94,
    "group_cohesion": 0.91
  },
  "relations": {
    "group_type": "family",
    "role": "member",
    "leader_id": "p102"
  },
  "state": {
    "local_density": 5.8,
    "nearby_people": 21,
    "stress": 0.67,
    "event_phase": "emergency"
  }
}
```

大模型不直接修改位置，而是输出标准化动作：

```json
{
  "action": "follow_group",
  "target": "p102",
  "speed_mode": "cautious",
  "route_preference": "low_density",
  "reason": "保持家庭群体并避开高密度区域"
}
```

运动引擎验证动作是否合法，再将其转换为速度、方向、等待、跟随或绕行行为。模型失败时使用本地规则，仿真不能因外部模型不可用而停止。

---

## 8. 论文—字段溯源矩阵

| 字段或机制 | 主要来源 | 证据类型 | 当前结论 |
|---|---|---|---|
| `base_speed`/期望速度 | Moussaïd 2011及行人实验 | 行为模型与实验 | 机制可用，分布需按场景标定 |
| `body_radius`、接触、摩擦 | Helbing 2000 | 机制模型 | 待实现二维物理层 |
| `personal_space` | Gorrini 2014；Sorokowska 2017 | 行人实验、跨国调查 | 可作先验，不可等同碰撞距离 |
| 视觉感知与方向选择 | Moussaïd 2011 | 认知行为模型 | 待实现二维视野 |
| `group_size` 和队形 | Moussaïd 2010 | 现场实证 | 可用于群体规模和队形校准 |
| `group_cohesion` | Qiu 2010；Ivo 2021 | 群体模型 | 待接入显式群体力 |
| `leader_id` 和跟随 | Leader–follower 模型 | 行为模型 | 待实现 |
| 关系强度 | Qiu 2010；Ivo 2021 | 群体模型 | 待实现关系图 |
| 情绪敏感性与恢复 | Cao 2023；OCEAN-SIS | 机制模型 | 已部分实现，数值待标定 |
| 社会决策阈值 | 社会传染阈值模型 | 网络机制模型 | 待实现 |
| `cultural_group` | 项目场景设计 | 分类变量 | 仅用于条件化参数分布 |
| `same_culture_attraction` | 项目假设 | 待验证 | 不应宣称为经验常数 |
| `authority_compliance` | 项目文档 | 待调查 | 保留给决策模型 |
| `language_delay` | 项目文档 | 待实验 | 保留给决策模型 |
| `information_trust` | 项目文档 | 待问卷 | 保留给决策模型 |
| `conflict_threshold` | 项目文档 | 待验证 | 暂不进入物理模型 |

必须坚持：论文支持某种机制，不代表论文提供了本项目当前使用的全部具体数值。

---

## 9. 当前实现与下一步

### 已实现

- 异质 Agent 属性与文化画像采样；
- 局部人数和密度感知；
- 个人密度容忍阈值；
- 压力、疲劳和邻居压力传播；
- 通用事件对密度、速度、压力和规避的影响；
- 本地规则和可替换的大模型决策接口；
- 决策对速度、等待和绕行的反馈；
- PedNStream 宏观路段密度反馈。

### 优先补充

1. 创建 `AgentGroup` 和 `AgentRelation`；
2. 将邻居识别升级为二维空间索引；
3. 实现个人空间、碰撞规避和接触力；
4. 实现群体队形、领队跟随、掉队等待和重组；
5. 让已有文化属性通过群体、信息和决策机制产生可解释作用；
6. 建立参数配置、实验标定和敏感性分析流程。

---

## 10. 核心参考文献

1. Helbing, D., Farkas, I., & Vicsek, T. (2000). Simulating dynamical features of escape panic. *Nature*, 407, 487–490. https://doi.org/10.1038/35035023
2. Moussaïd, M., Perozo, N., Garnier, S., Helbing, D., & Theraulaz, G. (2010). The walking behaviour of pedestrian social groups and its impact on crowd dynamics. *PLOS ONE*, 5(4), e10047. https://doi.org/10.1371/journal.pone.0010047
3. Moussaïd, M., Helbing, D., & Theraulaz, G. (2011). How simple rules determine pedestrian behavior and crowd disasters. *PNAS*, 108(17), 6884–6888. https://doi.org/10.1073/pnas.1016507108
4. Qiu, F., & Hu, X. (2010). Modeling group structures in pedestrian crowd simulation. *Simulation Modelling Practice and Theory*, 18(2), 190–205. https://doi.org/10.1016/j.simpat.2009.10.005
5. Ivo, D., Cavalcante-Neto, J., & Vidal, C. (2021). A model for flexible representation of social groups in crowd simulation. *Computers & Graphics*, 101, 7–22. https://doi.org/10.1016/j.cag.2021.08.005
6. Sorokowska, A., et al. (2017). Preferred interpersonal distances: A global comparison. *Journal of Cross-Cultural Psychology*, 48(4), 577–592.
7. Cao et al. (2023). Modified social force model considering emotional contagion for crowd evacuation simulation. *International Journal of Disaster Risk Reduction*, 96, 103902. https://doi.org/10.1016/j.ijdrr.2023.103902
8. A threshold model of social contagion process for evacuation decision making. *Transportation Research Part B*, 45(10), 1590–1605. https://doi.org/10.1016/j.trb.2011.07.008
