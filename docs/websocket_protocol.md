# CrowdSim WebSocket 协议

后端默认监听 `ws://127.0.0.1:8765`，当前只允许一个客户端。所有坐标、速度和道路位置来自同一个 SUMO 时间边界。

## 兼容命令

| action | 语义 |
|---|---|
| `configure` | 唯一连接握手入口；首次初始化或 CLOSED/ERROR 后创建新运行；相同需求重复请求不重建；固定需求先记录并忽略 `count` |
| `set_speed` | 改变墙钟倍率，不改变固定的 SUMO `step_length` |
| `start` | 从 READY 或 PAUSED 开始/恢复 |
| `pause` | 在当前一致边界暂停 |
| `update_flood_source` | 更新外部危险输入，不直接修改人数或密度 |
| `set_event` / `trigger_event` | 触发信息或危险事件 |
| `event_decision` / `set_policy` / `apply_policy` | 提交引导或建议干预 |
| `set_group` | 登记真实 `member_ids`、可选 `leader_id` 与已配置的 `rendezvous_id` |

## 需求界定提交

需求界定页面通过同一 WebSocket 发送 `submit_requirement`。该操作只校验并以不可变 JSON
保存需求，不初始化 SUMO：

```json
{"action":"submit_requirement","request_id":"submit-...","requirement":{"schema_version":1}}
```

成功响应为 `requirement_accepted`，包含后端生成的 `requirement_id`、SHA-256 指纹、能力状态和
警告。前端后续在 `configure` 中发送该 ID：

```json
{"action":"configure","request_id":"configure-...","requirement_id":"req-..."}
```

后端读取 `runs/requirements/<requirement_id>.json`，以其中的总人数和画像分布初始化运行，并把
不可变快照复制到 `runs/<run_id>/requirement.json`。初始化成功后，`init.requirement` 返回需求的
`project`、`spatial_scope`、`population`、`scenario` 和 `observation` 信息；前端应以该响应和
`init.demand.planned` 作为运行界面的权威数据。当前只有 `memorial-tower` 对应的热点场景可
初始化，服务必须用 `--scenario hotspot` 启动；其他地点可以保存，但 configure 会明确拒绝而不是
静默回退到错误场景。

## 查询命令

- `get_status`：返回状态机、需求账本、引擎版本和能力状态，不推进仿真。
- `get_agent_state`：参数 `id`，返回同一个 `snapshot_id` 下的画像、内部状态和 SUMO 运动状态。
- `reset`：后端显式重建接口，先取消并等待旧循环退出，再关闭旧运行并建立新 `run_id`；生成式热点模式省略 count 使用场景默认人数，其他模式保留此前有效人数设置。前端“重置”按钮不发送此命令，而是清空界面并断开，下一次 configure 才建立新运行。

## 连接与重置生命周期

前端每次连接只发送一次带唯一 `request_id` 的 configure，等待匹配的 init 后允许 start。不能连续发送 reset 和 configure。init 回传 request_id、run_id 和真实 runtime_state；重复的同一 request_id 返回 duplicate_request，记录只在当前连接有效。

- CREATED：校验有效配置后初始化一次。
- CLOSED/ERROR：等待旧循环退出后重建一次；生成式热点模式无人数参数时使用场景默认值，其他模式保留此前有效设置。
- READY：相同需求只返回现有 init，不新建目录；改变可配置需求必须显式 reset。
- RUNNING/PAUSED：不重建运行，人数变更拒绝；播放参数可以更新。
- FINISHED：返回完成状态，不因重复握手自动重开实验。

`demand.count_configurable` 同时反映场景模式和原需求文件的能力。内置热点预设使用 `generated_hotspot`，接受 0～10000 的整数 count，省略/null 使用配置中的 visitor_count（当前 700）。人数是整轮热点访客总数，背景人数固定为 0，不是同时在场人数。每轮重新分配刷新时间和空间位置，不复制固定的 700 人需求。空场实验之后仍可重置为非零人数。一般 `configurable` 模式和显式 `fixed` 模式保留原行为：仅 personFlow 不支持 count；fixed 记录并忽略合法 count。unsupported_demand_count 错误包含 demand 能力。

生成式热点初始化先发送匹配 request_id 的 `preparing`，完成后才发送 `init`。生成/初始化在串行等待的工作线程内完成，期间 WebSocket 可以处理 ping/pong，但不接受并行修改运行。前端初始化期限为 120 秒；只有匹配的 init 才解锁运行按钮。其他模式保留 init 作为首个成功握手响应。

断开连接时先取消并等待异步循环，再关闭 runtime，最后释放客户端占用并清理请求记录；即使关闭出错，也释放客户端槽位。新连接不得使用旧运行帧。前端关闭中禁止重连，旧连接回调及不同 run_id 的帧被忽略；初始化错误或超时后关闭连接，允许用户重试。

暂停保留运行，重置结束运行。当前客户端不发送 clear_event，也不通过新增 clear_event 别名掩盖生命周期问题。界面启动/暂停状态以 command_result 确认为准；重置停止本地积水同步，清空旧帧、图表、热力与事中 3D 实体插值，保留可复用的输入设置。

协议验证：`python -m unittest tests.test_websocket_contract tests.test_simulation_loop -v`；前端目录执行 `node --test tests/crowdSimConnection.test.mjs`。测试覆盖真实 WebSocket 断开重连、固定人数重复 configure、任务取消等待、失败清理、迟到消息和超时重试。真实浏览器页面仍需人工联调。

## 响应

- `init` 保留 `center`、`speedFactor`、`step_length`、`real_step_interval`、`flood_points`、`flooded_roads`、`metrics`，并输出 `request_id`、`run_id`、`runtime_state`、`scenario` 时间轴与 `demand` 能力。
- `update` 保留 `step`、`step_seconds`、`step_index`、`step_length`、`speed_factor`、`vehicles`、`pedestrians`、`events`、`metrics`、`event_state`，并重复输出 `runtime_state` 和 `scenario` 以便客户端识别正常完成及恢复上下文。
- 热点预设的 `scenario.timeline_end_seconds=1800`、`timeline_step_count=3600`，并通过 `scenario.active_hotspot_id` 指明当前需求对应的热点。`demand.mode=generated_hotspot` 返回 `requested_count`（省略时为 null）、`effective_count`、`planned`、`default_count`、`count_min`、`count_max`、`count_step`、`count_scope=total_hotspot_visitors`、`background_count=0`。显式 fixed 模式仍将忽略值记录在 `requested_count_ignored`。
- 每帧 `metrics.population` 的 planned/departed/active/arrived 分别表示计划总数、累计已刷新、当前在场、累计完成全部行程；待刷新为 planned-departed。arrived 不是“已到达热点”，而是已经结束整个入场—停留—离场行程。
- 热点运行到 `timeline_end_seconds` 后，最终 `update.runtime_state=FINISHED`；客户端应将进度显示为 100%，并保留最终帧供查看。
- `metrics.hotspot_metrics.<热点ID>.core_process_state` 只描述纪念塔环道，输出 `normal → building → congested → dispersing → cleared`。安全口径的 `process_state` 还读取未完成访客以及园内、园外入口低速积压；环道清空但外围仍拥堵时进入 `residual_congestion`，不得误报 `cleared`。两者都输出中文标签、判定原因、近窗趋势、历史峰值和阶段切换时刻。
- 热点指标按 `core`（纪念塔外圈两条地面环道边）、`entries`（纪念塔两个入口）、`park`（黄浦公园道路）、`park_entries`（公园南北入口）和 `external_approach`（园外接近道路）分别输出人数、面积、密度、速度及分边统计；内侧地面环道和更深层环道已从热点目标、路线和核心测量中排除。两个入口层级分别记录累计进出流量。`visit_lifecycle` 输出未出发、园外接近/排队、园内接近/排队、进入环道、聚集、离场、完成、未完成和完成比例。
- `walking_count`、计划停留阶段的 `stopped_count` 和低于 `slow_walking_threshold_mps`（当前为 0.5 m/s）的 `slow_walking_count` 必须分开，不得把计划停留造成的零速度直接解释为拥堵。
- 行人的 `edge` 是真实 SUMO 边；`display_edge` 在内部边期间提供最近普通道路；`data_source` 固定为 `sumo_simulation`。
- `get_status.routing` 和 `metrics.routing` 输出 POI 寻路请求、缓存命中、拓扑拒绝、目标回退及执行冷却/失败的聚合统计，具体定义见 [POI 重规划补强](poi_replanning.md)。不可达候选不会删除行人，执行后无法恢复的部分失败会令运行进入 `ERROR`。
- 行人 `state` 包含 `nationality`、`native_language`、`decision`、`decision_reason`、可空的 `decision_confidence` 与 `decision_source`；置信度来自经过校验的 LLM 输出，规则计划可为 `null`。
- `synthetic` 暂保留为兼容字段，固定为 `false`，已废弃，模型与统计不读取它。
- 事件中的 `density_multiplier` 若输出，固定为 `1.0` 且标记废弃，任何计算不得读取。
- 事件、危险、干预和群组命令先返回 `queued`；它们在下一仿真步边界生效后返回含 `applied_at` 与 `snapshot_id` 的 `applied` 或 `rejected` 结果。暂停状态下立即在当前边界处理。
- 错误统一为 `{"type":"error","code":"...","message":"...","request_id":...}`。

## 当前迁移状态

协议文档描述最终合同。实施记录中尚未完成的 action 必须返回 `not_implemented`，不能伪装为成功。真实前端联调完成前，F15 只能标记为后端合同通过、整体部分完成。
