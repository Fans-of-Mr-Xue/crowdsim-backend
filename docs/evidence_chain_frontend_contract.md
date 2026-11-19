# 个体决策证据链 · 前端加法契约（v1）

> 适用后端仓：`/Users/bytedance/Documents/WWW/MACE-Platform/crowdsim-backend`
> 运行端口：runtime 进程默认 `8765`（WS 与下列只读 HTTP 同端口同源，`/api/v1/...`）。
> 本批字段均为**加法**：既有帧/档案/计划/回执字段不改名、不移除；证据链字段各自带版本号。
> 所有值来自后端生产结构的只读投影；**缺失阶段显式 `null` / `missing`，禁止 LLM 或前端追认、补写、拼接**。
> 博弈机制固定命名 `utility_weighted_probabilistic`（效用加权概率选择，quantal/softmax 响应），**不是**完整策略博弈/纳什均衡。

## 0. 三条不可降级口径

1. **只读**：证据链构建与 counterfactual 对照不重新评估情感/欲望、不调用决策主体、不打分或更新学习者、不连 SUMO、不重设 RNG、不改计数。
2. **隔离**：counterfactual 对 profile/state/observation/候选集先深拷贝再计算；正式 state、learner、SUMO、RNG、计数均不触碰。不满足隔离条件时返回 `available:false` + `reason`，不强行出对照。
3. **来源归属**：`llm` 块只反映控制器既有诊断；`learning.events` 只 relate 控制器已发出的审计事件；未发生的更新/样本/回执一律 `null`/空列表。

---

## 1. 端点一览（GET，只读）

| 方法 | 路径 | 用途 | 查询参数 |
|---|---|---|---|
| GET | `/api/v1/decision-traces` | 按 run/人/快照/时刻过滤的轨迹列表 | `personId`, `snapshotId`, `timeSeconds`（数值；非法返回 400 `invalid_time_seconds`） |
| GET | `/api/v1/decision-traces/{person_id}` | 某人最新一条轨迹 | 路径参数 `person_id`；无记录返回 404 `trace_not_found` |
| GET | `/api/v1/role-summary` | 按角色聚合的效果汇总 | 无 |
| GET | `/api/v1/counterfactual` | 单机制隔离对照（仅当前边界） | `personId`（必填，缺 400 `missing_person`）, `mechanism`（白名单见 §4）, 可选 `snapshotId` / `timeSeconds`（定位基线帧；非法时刻 400 `invalid_time_seconds`，无记录 404 `trace_not_found`）；人不存在 404 `unknown_agent` |

runtime 进程（内存同源索引）列表响应：

```json
{
  "version": "decision-trace-v1",
  "run_id": "<run_id>",
  "count": 0,
  "traces": [ /* decision-trace-v1 对象，结构见 §2 */ ]
}
```

### 1.1 control 进程持久档案回读（append-only JSONL）

control 进程前缀 `/crowdSim/control`，按 `run_id` 关联 runtime 目录的 `decision_traces.jsonl`；只读，不写控制集合。旧档案无证据时 `missing` 给出原因，不造行。

| 方法 | 路径 | 查询参数 | 用途 |
|---|---|---|---|
| GET | `/crowdSim/control/runs/{run_id}/decision-traces/persons` | `runtimeRunId` | 轻量人员列表（只取索引边界字段，不回读轨迹正文），供历史列表页 |
| GET | `/crowdSim/control/runs/{run_id}/decision-traces` | `personId`, `snapshotId`, `timeSeconds`, `runtimeRunId`, `pageSize`, `cursor` | 档案轨迹列表，可按人/快照/时刻/指定 runtime 目录过滤；分页返回 bounded 页 |
| GET | `/crowdSim/control/runs/{run_id}/decision-traces/{person_id}` | `snapshotId`, `timeSeconds`, `runtimeRunId`, `pageSize`, `cursor` | 某人档案轨迹（分页同上） |
| GET | `/crowdSim/control/runs/{run_id}/role-summary` | `runtimeRunId` | 档案轨迹角色聚合（同 §5 结构）；首次未就绪返回 `warming`，后台构建，前端轮询同端点 |

轨迹响应体：`{ "run_id", "runtime_run_id", "selected_runtime_run_id", "previous_runtime_run_ids", "mixed_run": false, "trace_file": "decision_traces.jsonl", "missing": [原因], "traces": [/* decision-trace-v1 */], "role_summary": role-summary-v1 | null, "total": number, "next_cursor": number | null }`。不带分页参数时行为同旧口径：无快照/时刻过滤附带 `role_summary`，带过滤为 `null`，`total`/`next_cursor` 可缺省。

分页：`pageSize` 为正整数，服务端钳制到 1–50（默认 50）；`cursor` 为上一页返回的 `next_cursor`（起始偏移，从 0 计）。`total` 为该过滤条件下匹配边界总数；`next_cursor` 为下一页起始偏移，末页为 `null`。完整档案不截断——逐页携带相同过滤条件与返回的 `cursor` 即可取全。分页路径不附带 `role_summary`（汇总仍需全量扫描，走 role-summary 端点）。

人员列表响应体：`{ "run_id", "runtime_run_id", "selected_runtime_run_id", "previous_runtime_run_ids", "mixed_run": false, "trace_file": "decision_traces.jsonl", "missing": [原因], "total": number, "persons": [ { "person_id", "role_id", "trace_count", "first_time_seconds", "last_time_seconds" } ] }`。`persons` 按 `person_id` 排序；`role_id` 取该人最近一条轨迹的角色，无轨迹字段时为 `null`。

角色汇总响应体：`{ "run_id", "runtime_run_id", "selected_runtime_run_id", "previous_runtime_run_ids", "mixed_run": false, "trace_file": "decision_traces.jsonl", "missing": [原因], "traces": [], "total": 0, "next_cursor": null, "warming": boolean, "role_summary": role-summary-v1 | null }`。汇总由流式 reducer 在进程内按档案身份 `(size, mtime_ns)` 缓存：首次构建前 `warming: true`、`role_summary: null`、`missing` 含 `summary_warming`，服务端后台折叠档案，前端按相同参数轮询本端点，直到 `warming: false` 并带回 `role_summary`；命中缓存后不再解码整份档案。档案缺失时 `role_summary: null`、`warming: false`，原因见 `missing`。

`runtimeRunId` 显式选择一个归属本 run 的 runtime 档案目录：当前 `runtime_run_id` 或历史 `previous_runtime_run_ids` 之一；只回读该目录，**不跨 run 合并**。传入不属于本 run 的 id 返回 `RUNTIME_RUN_ID_NOT_OWNED`。不传时默认当前 runtime 目录。runtime 进程内联内存端点只挂当前 runtime 索引，历史档案回读走本 control 端点。control 只读归档，不挂 counterfactual 空接口；真实隔离对照只在 runtime 8765 入口计算。

---

## 2. `decision-trace-v1` 轨迹对象

### 2.1 顶层字段

| 字段 | 类型 | 说明 / 缺失口径 |
|---|---|---|
| `version` | string | 固定 `"decision-trace-v1"` |
| `run_id` | string | 运行标识 |
| `snapshot_id` | string | 快照标识 |
| `time_seconds` | number \| null | 决策边界时刻；非有限值置 `null` |
| `person_id` | string | 个体标识 |
| `role_id` | string | 角色标识（`crowd_role`） |
| `contract` | string \| null | 当前执行合同 |
| `boundary_ok` | boolean | state/obs/快照/时刻是否与当前运行边界一致；不一致时下游阶段降级为缺失 |
| `observation` | object | 决策边界（before-decision）观测投影，见 §2.2（G7 邻域组成） |
| `motion` | object | 决策边界自身运动快照投影，字段见 §2.2 motion |
| `boundary` | object | 决策边界段（before_decision），见 §2.1.1 |
| `post_execution` | object | 动作施加后段（after_decision），见 §2.1.2 |
| `state` | object \| `{}` | 执行后内部状态投影（兼容字段，等同 `post_execution.state`），见 §2.3 |
| `affect` | object | PAD/欲望投影，见 §2.4 |
| `goal_intent` | object \| null | 目标意图链路，见 §2.5；边界不一致时为 `null` |
| `game` | object | 有限理性博弈表，见 §2.6（G1） |
| `candidates` | array | 合法候选身份/成本卡片，见 §2.7（G2） |
| `candidate_scores` | object | 控制器既有候选评分结构；键 `candidate_id`，值沿用控制器写入结构；无评分时为 `{}` |
| `llm` | object \| null | LLM 驱动诊断；角色未装配 decision 模块时为 `null`，见 §2.8 |
| `plan` | object \| null | 最终计划（BehaviorPlan 投影），见 §2.9；无计划为 `null` |
| `constraints` | object \| null | 事后约束投影（深拷贝），无约束为 `null` |
| `receipt` | object \| null | SUMO/执行回执；边界不一致或无证据为 `null`，见 §2.10（G4） |
| `learning` | object | 学习审计块，见 §2.11（G3） |
| `participation` | array | 各臂/候选参与状态，见 §2.12（G4/G5） |
| `guard` | object | 保护门禁，见 §2.13 |

### 2.1.1 `boundary`（决策边界段，before_decision）

决策前快照，与执行后结果分段存放，不混一张表。

| 字段 | 类型 | 说明 |
|---|---|---|
| `phase` | string | 固定 `"before_decision"` |
| `snapshot_id`, `time_seconds` | string / number \| null | 边界归属 |
| `observation` | object | 边界观测，结构同 §2.2 |
| `motion` | object | 边界自身运动，结构同 §2.2 motion |
| `state` | object \| null | 决策前边界状态投影；来源为生产 PAD 冻结上下文，或 runtime 在解析/施加前冻结的同边界轻副本；身份不匹配时为 `null`，结构同 §2.3 |
| `state_status` | string | `frozen_boundary`（生产管线冻结上下文）/ `runtime_before_snapshot`（runtime 决策前冻结副本）/ `missing_before_state`（before-state 未留存，不拿执行后状态冒充）/ `boundary_not_confirmed`（边界未确认） |

### 2.1.2 `post_execution`（动作施加后段，after_decision）

动作应用之后留下的状态/计划/回执/约束。

| 字段 | 类型 | 说明 |
|---|---|---|
| `phase` | string | 固定 `"after_decision"` |
| `snapshot_id`, `time_seconds` | string / number \| null | 归属 |
| `state` | object | 执行后状态投影，结构同 §2.3 |
| `plan` | object \| null | 执行后当前计划，结构同 §2.9 |
| `receipt` | object \| null | 执行回执，结构同 §2.10 |
| `constraints` | object \| null | 事后约束，结构同 §2.13 约束口径 |

### 2.2 `observation`（G7 邻域组成）

`status`：`"observed"` 或 `"missing_observation"`。缺失时仅 `{"status":"missing_observation"}`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `snapshot_id` | string | 观察快照 |
| `time_seconds` | number | 观察时刻 |
| `neighbour_ids` | string[] | 邻域个体 |
| `local_people_count` | number | 邻域人数 |
| `local_area_m2` | number \| null | 邻域面积 |
| `objective_density_per_m2` | number \| null | 客观密度 |
| `perceived_crowding` | number \| null | 感知拥挤 |
| `perceived_risk` | number \| null | 感知风险 |
| `density_level` | string | 密度档位 |
| `flood_impact` | number \| null | 积水影响 |
| `event_impact` | number \| null | 事件影响 |
| `known_event_ids` | string[] | 已知事件 |
| `available_goal_ids` | string[] | 可达目标集合 |
| `motion` | object | 自身运动：`person_id, time_seconds, x, y, lon, lat, speed, edge_id, lane_id, lane_position, angle, stage_index, stage_type, remaining_stage_count, departed`（存在才出现；非有限数值置 `null`） |

### 2.3 `state`（内部状态投影，存在字段才出现）

`person_id, stress, fatigue, perceived_risk, perceived_crowding, blocked_duration, activity_state, current_goal, pending_goal, poi_plan_active, activity_plan, next_decision_time, hotspot_entry_edge, hotspot_activity_phase, group_id, companion_ids, rendezvous_id`。

### 2.4 `affect`

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | string | `evaluated` / `disabled` / `not_enabled` / `no_role_config`；非 `evaluated` 时 `pad`、`desire` 为 `null` |
| `evaluated_at` | object \| null | `{"snapshot_id","time_seconds"}` |
| `emotion_enabled` | boolean | 该角色是否装配情感模块（能力闸门口径，不等于当前边界已评估） |
| `desire_enabled` | boolean | 该角色是否启用情感驱动欲望 |
| `continuity` | string | `first_boundary`（首帧无先前 PAD）/ `inertial_update`（相邻时刻续更）/ `disabled`（情感未启用） |
| `before_pad` | object \| null | 本帧更新前相邻时刻 PAD；首帧或未留存为 `null`，不得前端补推 |
| `after_pad` | object \| null | 本帧更新后 PAD；与 `pad` 同源 |
| `pad` | object \| null | `pleasure, arousal, dominance, label` |
| `desire` | object \| null | `{"scores": {欲望名: 分}, "primary": 主导欲望名}` |
| `input_factors` | object \| null | 进入 PAD 平滑的真实边界输入快照：`risk, crowding, stress, fatigue, blockage, speed_ratio`；情感模块未启用为 `null` |

### 2.5 `goal_intent`（G4 目标/动作建议链）

`null` 表示该边界无可追溯目标意图。字段：`snapshot_id, time_seconds, primary, target_ids[], local_action, reason, locked, selected_goal, reachable_goal_ids[], route_failures[]`。

### 2.6 `game`（G1 有限理性博弈表）

| 字段 | 类型 | 说明 |
|---|---|---|
| `mode` | string | `utility_weighted_probabilistic`；未装配 game 模块或无可投影表时为 `unavailable` |
| `rationality` | number \| null | 理性参数；不可用时 `null` |
| `options` | array | 每候选臂一行；不可用时 `[]` |
| `selected` | object \| null | 生产边界实际抽样选中；无生产留存时为 `null`，**不得用 `recommended` 顶替** |
| `selected_reason` | string \| null | 无生产选中时的缺失原因（`evaluated_affect_boundary_missing` / `game_model_not_configured` / `game_model_has_no_readonly_table` / `game_table_recompute_failed` / `no_production_choice_recorded`） |
| `selection_source` | string \| null | `production_frozen`（生产冻结表与抽样，**与情感/欲望是否评估解耦**：情感关闭的角色照常投影真实 options/抽样）/ `mirror`（纯函数镜像重算，需已评估 PAD/欲望边界）；不可用为 `null` |
| `action_bias` | object \| null | 生产边界留存的 replay 动作偏置；镜像重算不带偏置，生产边界带偏置时该镜像表判定不可比 |
| `affect_evaluated` | boolean | 仅表示该表是否基于已评估冻结情感/欲望边界；**不代表 game 表本身是否可用** |
| `recommended` | object \| null | 按概率（并列按效用）取最高的一行建议；**仅表内排序建议，不代表抽样已选中** |

`options[]` 字段：`action, target_id, candidate_id, utility, probability, reason`。
`selected` 投影：`action, target_id, candidate_id, utility, probabilities, reason`（`probabilities` 为该次抽样的臂概率字典）。
`recommended` 投影：`action, target_id, candidate_id, utility, probability`。

### 2.7 `candidates`（G2 候选身份/成本）

数组，每项：

| 字段 | 类型 | 说明 |
|---|---|---|
| `candidate_id` | string \| null | 候选标识 |
| `action` | string | `reroute` / `change_goal`（`target_kind=="activity"` 为 `change_goal`） |
| `target_id` | string | 目标标识 |
| `target_kind` | string | 目标类型 |
| `target_edge` | string | 目标边 |
| `entry_edge` | string \| null | 入口边 |
| `edges` | string[] | 到达边序列 |
| `arrival_position` | number \| null | 到达位置 |
| `estimated_cost_seconds` | number \| null | 预估成本（秒） |
| `base_cost_seconds` | number \| null | 基础成本 |
| `congestion_delay_seconds` | number \| null | 拥堵延误 |
| `entry_density_person_per_m2` | number \| null | 入口密度 |
| `activity_duration` | number \| null | 活动停留时长 |
| `next_target_id` | string \| null | onward 下一目标 |
| `next_route_edges` | string[] | onward 边序列 |
| `next_arrival_position` | number \| null | onward 到达位置 |
| `minimum_savings_seconds` | number \| null | 最小可节省 |
| `switch_cooldown_seconds` | number \| null | 切换冷却 |

### 2.8 `llm`

| 字段 | 类型 | 说明 |
|---|---|---|
| `decision_source` | string | 固定 `"llm_driven"` |
| `callable` | boolean \| null | 连接器配置且熔断关闭才为可调用（门禁），不读 key 值 |
| `online_mode` | string \| null | `external` / `local_qa` / `unconfigured` |
| `successes`, `calls`, `fallbacks` | number \| null | 控制器计数 |
| `advisory_score_count` | number \| null | advisory 评分条数 |
| `last_error` | string \| null | 最近错误，无则 `null` |

### 2.9 `plan`（最终计划，BehaviorPlan 投影）

主要键：`person_id, proposed_action, target_id, candidate_id, selected_entry_edge, activity_duration, next_route_edges[], next_arrival_position, next_target_id, wait_until, speed_limit, preserve_future_stages, reason, description`（按计划类型存在）。无计划为 `null`。

### 2.10 `receipt`（G4 实际动作回执）

| 字段 | 类型 | 说明 |
|---|---|---|
| `person_id` | string | |
| `requested_action` | string | 请求动作；契约别名，回映 `proposed_action` |
| `proposed_action` | string | 原生产字段保留 |
| `applied_action` | string | 实际施加动作 |
| `status` | string | `applied` / `partial_failure` / `rejected` |
| `time_seconds` | number | 回执时刻；契约别名，回映 `applied_at` |
| `applied_at` | number \| null | 原生产字段保留 |
| `reason` | string | 失败/说明原因，成功可为空 |

边界不一致或无执行证据时为 `null`。别名仅在原字段存在时回填，不覆盖既有值。

### 2.11 `learning`（G3 真实 sample / replay / update 前后 / 下次选择）

未接控制器：`{"attached": false, "events": []}`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `attached` | boolean | 是否挂载学习控制器 |
| `method_id` | string \| null | 学习方法 |
| `integration_mode` | string \| null | 接入模式 |
| `interval_mode` | string \| null | 区间模式 |
| `accepted_samples` | number | 已接受样本数 |
| `update_version` | number | 更新版本 |
| `selections` | number | 选择次数 |
| `binding` | object | `binding_fingerprint, frozen` |
| `replay` | object \| null | 回放缓冲只读投影（仅回放方法有意义）；`buffer` 子块为真实缓冲摘要，不导出特征全量 |
| `events` | array | 控制器审计事件 relate，见下；归档回读时以 normalized join 结果为准 |
| `last_update` | object \| null | 最近一条 `learning_update` 事件；归档回读时以 normalized join 结果为准 |
| `linked_intervals` | array | 跨 boundary 只读关联（origin 决策 → 反馈后终态样本 → 下一选择），见下；未绑定/不可用分支为 `[]` |
| `aggregation_excluded` | boolean | 归因是否被排除出角色汇总；`true` 时本块计数已置零/空，不得按活跃学习状态计入角色汇总或界面活跃态 |
| `exclusion_reason` | string \| null | 排除原因：`binding_proof_unavailable`（sidecar 身份证明不可用）/ `no_personal_learner_binding`（无本人个人学习者绑定）/ `binding_method_mismatch`（本人绑定方法与角色方法不一致）；未排除为 `null` |
| `source_invalid_counts` | boolean | 持久化块自称 `attached`，但本人在归档选择集合中无记录，其源计数不可信 |
| `raw` | object | 该帧持久化学习块的历史副本，仅审计保留；不作为活跃学习状态、不计入角色汇总计数 |

`replay.buffer`：`{pending_or_replay_samples: number, sample_ids: string[], samples[]}`。`samples[]` 为真实缓冲样本的只读投影（`sample_id, action, target_id, candidate_id, state, features, next_state, next_actions, reward, duration_seconds, evidence_id, feedback_rule_id, feedback_version, predicted_cost_seconds, observed_cost_seconds, cost_complete, terminated, truncated`），不导原始 prompt/key；缓冲配置（`enabled/capacity/batch_size`）平铺在 `replay` 顶层。

`events[]` 按 `kind` 区分。每条事件顶层归属字段：`interval_id`（可空）、`snapshot_id`、`time_seconds`（数值时刻，非有限为 `null`），供历史回连同 run/人/快照/时刻。

- `selection`：`policy, baseline_action, baseline_target_id, baseline_candidate_id, selected_action, selected_target_id, selected_candidate_id, scores[]`。
- `learning_update`：`interval_id, acceptance, accepted_sample{action, target_id, candidate_id, reward, evidence_id, predicted_cost_seconds, observed_cost_seconds, cost_complete, next_actions[]}, evaluation{reward, end_reason}, before_encoding, next_encodings[], updates[]`。
  - `updates[]` 为核心更新细节；每条附 `sample` 不可变载荷投影（字段口径同 `replay.buffer.samples[]`），标注该次更新实际消费的样本；LinUCB 臂含 `action, previous_theta, theta, previous_b, new_b`（首臂 `previous_theta` 为 `null`）；bounded 含 `action, old, new, residual_seconds`；tabular 含 `action, state, old, new, target/bootstrap/effective_discount`。A 矩阵全量不导出。
- `interval`：`interval_id, trainable, end_reason, reward, duration_seconds, risk_exposure_seconds`。
- `sample_unavailable`：`interval_id, reason`。
- `execution_not_applied`：`receipt_status, receipt_reason`。
- `untracked_plan`：`reason`。

`linked_intervals[]`：completed-route / decision-interval 的终态样本可能落在 origin 决策之后的另一 boundary；该终态样本不塞回 origin 帧同 boundary 的 `events`，而是沿真实 `interval_id` 只读回连到本条关联。每条字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `interval_id` | string | 关联区间标识，与事件流 `learning_update.interval_id` / `interval.interval_id` 同源 |
| `cross_boundary` | boolean \| null | 反馈后时刻是否晚于 origin 决策时刻（容差 1e-6）；终态与 origin 同边界为 `false` |
| `origin_decision_key` | object | `{snapshot_id, time_seconds}`：origin 决策边界；缺快照为 `null` 字段值，不合成 |
| `observed_after_at` | object | `{snapshot_id, time_seconds, phase}`；`phase` 固定 `"feedback_after"`，表示反馈后终态边界 |
| `next_selection_key` | object \| null | `{record_id, snapshot_id, time_seconds}`：反馈后时刻之后本人第一条选择；无则 `null` |
| `accepted_sample` | object \| null | 该区间终态接受样本，样本字段口径同 `replay.buffer.samples[]`；无终态样本为 `null` |
| `source_timestamps` | object | `{started_at, ended_at}`：来源区间起止时刻，非有限为 `null` |
| `stage` | string | 固定 `"feedback_after"` |

关联只做只读投影，不重放、不跨 run 合并；`next_selection_key` 只取本人事件，不复制全局策略选择计数。

**归档归因门禁口径（只读投影，不改写持久化档案）**：

- 身份 proof（`summary.json` 个体绑定）只校验身份；`accepted_samples`/`update_version` 保留该帧记录的本人历史时序，末态档案总计数不回填到更早帧；`selections` 为本人选择事件在本帧时刻之前（`time_seconds <= 帧时刻`）的累计，不取全局末态总数。
- `events`/`last_update` 以同档案 normalized join（同 run、同人、同快照/时刻）结果为准；`integration_mode`/`interval_mode`/`replay` 等配置与回放元数据可回落展示，但事件与最近更新不回退到未校验的源块。
- `aggregation_excluded=true` 时计数已置零/空、`selections` 为 `null`；前端不得用 `raw` 中的自称计数恢复出一个假的已绑定学习者，也不得把该块计入角色汇总。
- `raw` 仅保留历史源块供审计对照；其 `attached`/计数是当时持久化快照，不代表当前活跃学习状态。`source_invalid_counts=true` 标记源计数与本人归档证据不符，展示原始源时需注明来源为归档 `raw`。
- 跨 boundary 终态样本沿 `linked_intervals` 真实 `interval_id` 回连，`cross_boundary` 按反馈后时刻是否晚于 origin 决策时刻判定；终态样本不塞回 origin 帧同 boundary 的 `events`。

### 2.12 `participation`（参与状态三态）

每项：`candidate_id, action, target_id, target_kind, state, reason`，可选 `policy`。

`target_kind`：`route` / `activity` / `local` / `learning_selection`。

`state` 枚举（见 §5.1）。

### 2.13 `guard`

`locked_itinerary`（boolean）、`protected_reasons[]`（`locked_itinerary, external_control, activity_dwell, policy_wait, flow_hold, hotspot_activity`）。

---

## 3. G1–G7 接缝字段映射

| 接缝 | 后端字段 |
|---|---|
| G1 有限理性每候选 utility/probability/rationality 及选中建议 | `game.options[].utility/probability`, `game.rationality`, `game.recommended`, `game.selected` |
| G2 候选 cost/delay/density/entry/target identity | `candidates[]` 全字段（成本/延误/密度/入口/目标身份） |
| G3 真实 sample/replay 及 scores/params old-new-residual | `learning.events`（selection.scores、learning_update.accepted_sample/before_encoding/next_encodings/updates）, `learning.linked_intervals`（跨 boundary 终态样本回连）, `learning.replay`, `candidate_scores` |
| G4 建议→细化→applied | `goal_intent` → `plan` → `receipt`；`participation` 中标 `downstream_refined` |
| G5 角色历史 PAD-desire/route completion/learning impact 分母 missing | `/api/v1/role-summary`（§5） |
| G6 只读 counterfactual 状态 | `/api/v1/counterfactual`（§4） |
| G7 obs 邻域组成/约束阻挡 | `observation` 邻域字段、`constraints`、`guard.protected_reasons` |

---

## 4. `counterfactual-v1` 对照对象

白名单 `mechanism`：`emotion`, `desire`, `game`, `learning`。
其余（含 `shared/frozen/inherit/marl/evolutionary/imitation/dqn/ppo/policy_clone`）一律 `available:false`，不近似。

各机制单变对照 scope（与后端实现一致，不改运行机制）：

- `emotion`：实测 PAD 替换为中性 PAD 基线；desire 与博弈表在该中性情感上重算。
- `desire`：保留 PAD；情感驱动欲望替换为普通规则回退分；博弈表按该欲望重算。
- `game`：affect/desire/learning 保持不变；变体去掉 §2.6 有限理性（bounded rationality）博弈选项表，仅报确认的规则/所有者基线臂，不做第二次博弈抽样。此处 `game` 指有限理性效用加权概率选择表，**不是**演化博弈、多方策略或均衡计算（完整博弈均衡见 §8 未承诺边界）。
- `learning`：affect/desire/game 保持不变；变体去掉学习建议分；不拷贝、观测或更新 learner 状态。

不可用时（`available:false`）字段：`version, available:false, mechanism, run_id, person_id, snapshot_id, time_seconds, reason`。

可用时：

| 字段 | 说明 |
|---|---|
| `version` | `"counterfactual-v1"` |
| `available` | `true` |
| `mechanism` | 单机制名 |
| `run_id, person_id, role_id, snapshot_id, time_seconds` | 边界归属 |
| `boundary` | `{locked_itinerary, candidate_count}` |
| `isolation` | `{cloned_profile, cloned_state, cloned_observation, cloned_candidates, formal_state_touched:false, formal_learner_touched:false, sumo_touched:false, rng_reseeded:false}` |
| `baseline` | 基线侧：`pad, desire, goal_intent, selected, top_game_option, game_option_count, candidate_scores` |
| `variant` | 变体侧：同结构 |
| `diff` | `{desire_primary_changed, goal_intent_changed, top_option_changed, learning_scores_dropped}` |
| `scope_note` | 固定隔离说明，原文 `"single-mechanism contrast on a frozen clone; no SUMO, learner update or RNG reseed."` |

`top_game_option`：`action, target_id, candidate_id, utility, probability`；空表为 `null`。`selected` 为基线生产抽样留存（无则 `null`）。

基线优先读取生产冻结表。镜像重算不带生产 replay `action_bias`：若边界带偏置而镜像表无偏置，该对照返回 `available:false`，reason 标明 bias 不可比，不声称复算生产决策表（emotion/desire 变体侧同口径）。基线冻结 trace 必须属于当前边界快照与时刻；陈旧快照（latest 上一帧）降级 `available:false`，reason 标明 stale snapshot。请求显式定位到某一历史帧（`snapshotId`/`timeSeconds` 与 runtime 当前边界不符）时，该帧冻结输入不由在线 runtime 持有、无法安全克隆或复算，返回 `available:false`，reason 固定 `historical_boundary_unavailable`；不得把当前边界结果写入历史记录。不带 `snapshotId`/`timeSeconds` 时对照当前边界最新基线。本地替代臂按生产门禁条件（外部/保持约束、活动行程、停留/离场阶段、行走阶段）判定是否开放，不一律放开。基线/变体侧模型求值或意图调用异常时同样 `available:false`，reason 为异常类型与信息，不 500 冒泡。

---

## 5. `role-summary-v1` 角色汇总对象

```json
{ "version": "role-summary-v1", "roles": { "<role_id>": { /* 见下 */ } } }
```

每角色：

| 块 | 字段 |
|---|---|
| `population` | `persons`（分母，含无轨迹者）, `with_trace`（有轨迹数） |
| `missing` | `no_trace, no_observation, affect_not_evaluated, learning_not_attached` |
| `actions` | `continue, slow_down, wait, reroute, change_goal, unknown` 计数 |
| `entries` | 入口/目标标识 → 选中计数（对象） |
| `wait` | `plans, cumulative_hold_seconds` |
| `route` | `route_plans, completed_route_evidence`（按 `interval_id` 去重） |
| `risk` | `avg_perceived_risk, avg_perceived_crowding, observations, risk_exposure_seconds, basis`（无观察时均值 `null`；暴露秒数按去重完成区间累计） |
| `basis` | 各块去重口径说明：`actions / wait / route / participation` |
| `affect_desire_timeseries` | `[{person_id, time_seconds, snapshot_id, desire_primary, pad}]`，按时刻/人排序 |
| `learning` | `accepted_samples, updates, selections, downstream_refined, participated_unchanged, constraint_blocked, protected_unchanged` |

口径：只统计轨迹已记录的内容。累计学习计数按人取该人最新一条轨迹的运行值，不跨帧重复累加；区间按 `interval_id` 去重；动作/等候/路线/入口按计划身份键（人、动作、目标、候选、边序列、到达位置、等待时刻、活动时长）去重，同一持久计划在多帧轮询中重复出现只计一次，等候秒数同一 wait 计划只加一次；参与记录按臂去重，保留该臂观测到的终态（unchanged→refined 转变记终态 refined，不被重复帧冲掉）；受保护未改选单列 `protected_unchanged`，不并入学习影响；不做自然收益外推、不按多种子/千人扩展。`risk.basis` 说明均值基于边界帧、暴露秒数基于完成区间；顶层 `basis` 说明各块去重口径。

### 5.1 参与状态枚举

- `participated_unchanged`：臂进入合法集，但最终计划保留基线臂 / 学习者未改选。
- `constraint_blocked`：臂被确认的保护挡在合法集外（锁定行程、活动停留、策略等待、流控保持、外部控制、热点活动）。
- `downstream_refined`：基线建议被决策主体或被追踪的学习选择细化到该合法候选/臂。

---

## 6. WS 帧内 `decision_trace` 摘要子块

`agent_state`/帧视图在已有结构内附只读摘要（完整块走 §1 HTTP 端点）：

| 字段 | 来源 |
|---|---|
| `version` | trace 版本 |
| `snapshot_id, time_seconds` | 边界 |
| `action` | `plan.proposed_action` |
| `target_id, candidate_id` | 计划 |
| `receipt_status` | `receipt.status` |
| `participation` | 各 `participation[].state` 字符串数组 |
| `learning_acceptance` | 最近 `learning_update.acceptance` |

边界不一致时该摘要子块不下发。

---

## 7. 降级与缺失规则（前端渲染约定）

- `boundary_ok:false`：observation/state 可能为旧边界，`goal_intent`、`receipt` 等已置 `null`；前端按缺失渲染，不回读旧帧补全。
- `affect.status != "evaluated"`：`pad/desire` 为 `null`。game 表是否可用与 affect 解耦：生产冻结表（`selection_source="production_frozen"`）照常投影真实 `options/selected`；只有纯函数镜像重算缺已评估边界时才 `game.mode="unavailable"`、`options=[]`、`selected=null`、`recommended=null`；不构造中性 PAD/欲望兜底表。
- `learning.attached=false`：未接控制器的空块 `events=[]`、计数为 0；不展示未发生的样本/更新。归档归因排除块（`aggregation_excluded=true`）的计数以 §2.11 门禁口径为准，`raw` 不计入活跃学习状态。
- 数值非有限一律 `null`；集合缺失为 `[]`，对象缺失为 `{}`，阶段缺失为 `null`。
- 端点 400/404 体为 `{"code","message"}`；counterfactual 不可用是 200 + `available:false`，不是错误。

## 8. 本契约**不**承诺的边界（未验/未覆盖）

- 跨 activity 的第二目标选择、onward 离场链闭环、外网上游 LLM 成功连通、可见窗口/GPU 像素呈现。
- 自然收益、多种子、千人规模扩展；受控经验 QA 的奖励标注为受控样本，不等同自然收益。
- 完整策略博弈/均衡计算；本契约仅效用加权概率选择。
- PAD 惯性连续性：本帧归档 `before_pad`/`after_pad` 与 `continuity`（首帧 `first_boundary`、续更 `inertial_update`）；跨时刻平滑公式本身不改写，首帧 `before_pad` 为 `null`。
