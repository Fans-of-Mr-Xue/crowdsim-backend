# CrowdSim WebSocket 协议

后端默认监听 `ws://127.0.0.1:8765`，当前只允许一个客户端。所有坐标、速度和道路位置来自同一个 SUMO 时间边界。

## 兼容命令

| action | 语义 |
|---|---|
| `configure` | 在首次启动前初始化；`speedFactor` 只改变墙钟播放倍率，`pushFps` 只限制推送频率 |
| `set_speed` | 改变墙钟倍率，不改变固定的 SUMO `step_length` |
| `start` | 从 READY 或 PAUSED 开始/恢复 |
| `pause` | 在当前一致边界暂停 |
| `update_flood_source` | 更新外部危险输入，不直接修改人数或密度 |
| `set_event` / `trigger_event` | 触发信息或危险事件 |
| `event_decision` / `set_policy` / `apply_policy` | 提交引导或建议干预 |
| `set_group` | 登记真实 `member_ids`、可选 `leader_id` 与已配置的 `rendezvous_id` |

## 查询命令

- `get_status`：返回状态机、需求账本、引擎版本和能力状态，不推进仿真。
- `get_agent_state`：参数 `id`，返回同一个 `snapshot_id` 下的画像、内部状态和 SUMO 运动状态。
- `reset`：显式关闭当前引擎并建立新 `run_id`。未显式 reset 时不会自动补人或重开实验。

## 响应

- `init` 保留 `center`、`speedFactor`、`step_length`、`real_step_interval`、`flood_points`、`flooded_roads`、`metrics`。
- `update` 保留 `step`、`step_seconds`、`step_index`、`step_length`、`speed_factor`、`vehicles`、`pedestrians`、`events`、`metrics`、`event_state`。
- 行人的 `edge` 是真实 SUMO 边；`display_edge` 在内部边期间提供最近普通道路；`data_source` 固定为 `sumo_simulation`。
- 行人 `state` 包含 `nationality`、`native_language`、`decision`、`decision_reason`、可空的 `decision_confidence` 与 `decision_source`；置信度来自经过校验的 LLM 输出，规则计划可为 `null`。
- `synthetic` 暂保留为兼容字段，固定为 `false`，已废弃，模型与统计不读取它。
- 事件中的 `density_multiplier` 若输出，固定为 `1.0` 且标记废弃，任何计算不得读取。
- 事件、危险、干预和群组命令先返回 `queued`；它们在下一仿真步边界生效后返回含 `applied_at` 与 `snapshot_id` 的 `applied` 或 `rejected` 结果。暂停状态下立即在当前边界处理。
- 错误统一为 `{"type":"error","code":"...","message":"...","request_id":...}`。

## 当前迁移状态

协议文档描述最终合同。实施记录中尚未完成的 action 必须返回 `not_implemented`，不能伪装为成功。真实前端联调完成前，F15 只能标记为后端合同通过、整体部分完成。
