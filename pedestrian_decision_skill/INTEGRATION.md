# Pedestrian Decision Skill：99 版后端接入说明

## 1. 接入结果

`PedestrianDecisionSkill` 已直接实现 `DecisionScheduler` 所需的决策引擎接口，并返回 `BehaviorPlan`。它不经过旧版 `(action, reason, source)` 三元组，也不直接调用 TraCI。

```text
SUMO 冻结快照
  → AgentProfile + AgentState + Observation + RouteCandidate
  → PedestrianDecisionSkill
  → BehaviorPlan
  → PlanExecutor 校验
  → TraCI 执行
  → SUMO 推进下一步
```

默认规则模式仍使用 `crowdsim.decision.AgentDecisionEngine`。只有显式选择 `--mode llm` 时才注入 Skill。

## 2. 接口合同

Skill 提供以下成员：

```python
enabled: bool
model: str

def rule_plan(profile, state, observation, candidates=()) -> BehaviorPlan:
    ...

async def decide(profile, state, observation, candidates=()) -> BehaviorPlan:
    ...

def diagnostics() -> dict:
    ...
```

因此现有 `DecisionScheduler` 的 2 秒周期、每个 tick 最多 12 次 LLM 调用、并发 4 和公平轮转均可直接复用。

## 3. 字段映射

| 模型字段 | 后端来源 |
|---|---|
| `agent_id` | `AgentProfile.person_id` |
| `snapshot_id` | `Observation.snapshot_id` |
| `time_seconds` | `Observation.time_seconds` |
| `profile.nationality` | `AgentProfile.nationality` |
| `profile.language` | `AgentProfile.native_language` |
| `profile.age_group` | `AgentProfile.age_group` |
| `current_state.status` | `AgentState.activity_state` |
| `current_state.speed` | `Observation.own_motion.speed` |
| `current_state.stress` | `AgentState.stress` |
| `current_state.fatigue` | `AgentState.fatigue` |
| `current_state.flood_impact` | `Observation.flood_impact` |
| `current_state.event_impact` | `Observation.event_impact` |
| `current_state.perceived_risk` | `Observation.perceived_risk` |
| `current_state.blocked_duration` | `AgentState.blocked_duration` |
| `surrounding_crowd.nearby_people` | `Observation.local_people_count - 1` |
| `surrounding_crowd.local_density` | `Observation.objective_density_per_m2` |
| `surrounding_crowd.density_level` | `Observation.density_level` |
| `surrounding_crowd.perceived_crowding` | `Observation.perceived_crowding` |

所有字段来自同一个 `snapshot_id`。周围人数不包含行人自己，客观密度仍包含观察区域中的本人。

## 4. 动作到 BehaviorPlan

模型只返回 `action/target_id/reason/confidence`。Skill 使用可信对象生成完整计划：

- `continue`：不附加执行参数；
- `slow_down`：速度上限为 `max(0.2, free_walking_speed × mobility × 0.65)`；
- `wait`：`wait_until = observation.time_seconds + 2.0`；
- `reroute`：目标必须属于 `RouteCandidate`，道路从候选复制；
- `change_goal`：目标必须是 activity 候选，活动时长和后续路线从候选复制。

路线计划也复制候选的 `arrival_position`；活动计划额外复制 `next_arrival_position` 和 `next_target_id`。这些字段由后端寻路和 POI 校验生成，模型不能提供或覆盖。活动完成后的目标推进、失败冷却与完整行程恢复由后端处理，见 [POI 重规划补强](../docs/poi_replanning.md)。

`PlanExecutor` 仍会验证 person、snapshot、动作、路线连通性和计划时效。模型永远不能直接提供道路、速度或等待时间。

## 5. 运行时注入

`SimulationRuntime` 新增两个可选参数：

```python
SimulationRuntime(
    config_path,
    decision_engine=PedestrianDecisionSkill(...),
    use_llm=True,
)
```

服务入口已经完成注入：

```bash
python crowdsim_overlay_server.py --mode llm
```

指定其他本地配置文件：

```bash
python crowdsim_overlay_server.py \
  --mode llm \
  --deepseek-config /absolute/path/to/config.json
```

实验脚本同样支持：

```bash
python scripts/run_experiment.py --mode llm --count 20 --steps 120
```

配置不可用时 Skill 的 `enabled` 为 `false`，调度器使用规则计划；具体原因可在 `metrics.decision_engine.last_error` 中查看。

## 6. 帧与实验记录

`BehaviorPlan.confidence` 会随计划进入实验记录。前端行人状态新增：

```json
{
  "nationality": "domestic",
  "native_language": "zh",
  "decision": "continue",
  "decision_reason": "状态稳定",
  "decision_confidence": 0.88,
  "decision_source": "llm"
}
```

API Key 不进入帧、诊断或实验记录。

## 7. 当前基础版限制

- 国籍当前主要是 `unspecified/domestic/international`，不是完整国家代码；
- 语言当前主要是 `unspecified/zh/en/other`；
- 只有后端成功生成候选时，模型才能选择 `reroute/change_goal`；
- 候选为空时，模型应选择 `continue/slow_down/wait`，非法路线动作会触发规则兜底；
- `slow_down` 的 65% 和 `wait` 的 2 秒是基础版工程参数，尚未做人群数据标定；
- Skill 按个体调用，成本与延迟由现有调度预算控制。

## 8. 验证命令

```bash
conda activate sumo
python -m pytest -q pedestrian_decision_skill/tests/test_skill.py
python -m pytest -q tests/test_skill_integration.py
python -m pytest -q
python -m pedestrian_decision_skill.examples.live_deepseek_test --live
```

前三条不调用真实 DeepSeek。最后一条只发送一次真实请求，并且只有 `BehaviorPlan.source == "llm"` 时才报告成功。
