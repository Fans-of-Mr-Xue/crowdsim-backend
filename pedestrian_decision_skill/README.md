# Pedestrian Decision Skill

基础版 DeepSeek 行人决策 Skill。它从 CrowdSim 的同一个冻结快照读取行人画像、当前状态、周围人群和合法候选目标，返回可由后端直接执行的 `BehaviorPlan`。

## 功能边界

Skill 负责：

- 构造并清洗模型上下文；
- 调用 DeepSeek Chat Completions API；
- 严格校验模型 JSON；
- 使用可信候选补齐 `BehaviorPlan`；
- API、配置或输出错误时生成确定性规则计划；
- 记录模型名、成功次数、兜底次数和最后错误。

Skill 不负责推进 SUMO、生成道路、管理人口、控制 WebSocket 或决定调用频率。决策周期、LLM 预算和并发由后端 `DecisionScheduler` 管理，计划由 `PlanExecutor` 再次校验后通过 TraCI 执行。

## 安装与配置

```bash
conda activate sumo
python -m pip install -r requirements.txt
```

真实配置位于 `pedestrian_decision_skill/config.json`，该文件已被 `.gitignore` 忽略：

```json
{
  "api_key": "填写 DeepSeek API Key",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-v4-flash",
  "timeout_seconds": 10,
  "max_tokens": 160,
  "temperature": 0.1
}
```

不要提交、打印或发送真实 API Key。可提交模板为 `config.example.json`。

## 输入

集成接口直接接收新版后端对象：

```python
plan = await skill.decide(profile, state, observation, candidates)
```

- `profile`：`AgentProfile`
- `state`：`AgentState`
- `observation`：`Observation`
- `candidates`：后端验证过的 `RouteCandidate`，可为空

发给模型的普通字典结构见 `examples/input.example.json`。核心字段包括：

- 国籍、语言、年龄组和个体容忍参数；
- 当前行为状态、速度、压力、疲劳、积水影响、事件影响和感知风险；
- 周围人数、客观局部密度、密度等级和主观拥挤；
- 候选目标 ID、类型和预计成本。

道路列表不会发送给模型。`nationality` 和 `language` 只允许作为有限背景信息，Prompt 明确禁止刻板推断。

## 模型输出

模型必须且只能返回四个字段：

```json
{
  "action": "reroute",
  "target_id": "exit_demo",
  "reason": "当前区域风险较高，选择可达出口",
  "confidence": 0.86
}
```

允许动作：

| 动作 | target_id | Skill 生成的计划 |
|---|---|---|
| `continue` | 必须为 `null` | 保持当前计划 |
| `slow_down` | 必须为 `null` | 按基础速度的 65% 生成速度上限，最低 0.2 m/s |
| `wait` | 必须为 `null` | 从当前快照起等待 2 秒 |
| `reroute` | 必须是候选 ID | 从候选复制可信的 SUMO 路线 |
| `change_goal` | 必须是 activity 候选 | 复制活动路线、停留时间和后续路线 |

`person_id`、`snapshot_id`、道路、速度、等待时间和 `source` 都由 Skill 使用后端数据填写，模型不能伪造。

## 兜底

以下情况使用 `source="rule_fallback"` 的规则计划：

- 配置文件缺失或 API Key 为空；
- 网络请求失败或超时；
- DeepSeek 返回非 JSON；
- 字段缺失、重复或多余；
- 动作不支持；
- 目标不存在或 `change_goal` 选择了非活动目标。

高风险且存在候选时选择候选目标；高风险但没有候选时减速；持续受阻时等待；无明显风险时继续。

## 运行

仅查看上下文和 Prompt，不发送请求：

```bash
python -m pedestrian_decision_skill.examples.basic_usage
```

发送一次真实 DeepSeek 请求：

```bash
python -m pedestrian_decision_skill.examples.live_deepseek_test --live
```

运行 Skill 测试：

```bash
python -m pytest -q pedestrian_decision_skill/tests/test_skill.py
```

启动集成后的后端：

```bash
python crowdsim_overlay_server.py --mode llm
```

规则模式仍是默认值：

```bash
python crowdsim_overlay_server.py --mode rule
```

完整后端接入说明见 `INTEGRATION.md`。
