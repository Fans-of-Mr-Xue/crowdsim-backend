# Pedestrian Decision Skill 后端适配说明

本文面向负责 CrowdSim 后端适配的开发者，说明如何把独立的
`pedestrian_decision_skill` 接入当前仿真。本文以仓库现有代码为准；Skill
内部实现、SUMO/PedNStream 原理和前端协议不在本文展开。

## 1. 接入目标与边界

Skill 负责：

- 接收单个行人的画像、当前状态和周围人群摘要；
- 调用 DeepSeek 选择一个高层动作；
- 严格校验模型输出；
- 配置、请求或模型输出失败时返回确定性的本地兜底结果；
- 返回统一的 `action/reason/confidence/source` 字典。

后端适配器负责：

- 从当前 `Agent` 构造 Skill 输入；
- 决定何时、为哪些行人发起决策；
- 把抽象动作应用到现有 Agent 和路线逻辑；
- 处理并发、仿真节奏、已离场行人和可能过期的结果；
- 根据需要记录不包含密钥的运行指标。

Skill 不直接导入或修改 `Agent`、`OverlayNetworkSimulator`、WebSocket、SUMO、
PedNStream、道路或路线对象。

## 2. 当前后端已有接入点

当前代码已经具备决策调度和动作应用框架：

- `crowdsim_models.py` 中的 `Agent` 保存感知状态和当前决策；
- `crowd_environment.py` 的 `update_perception()` 更新周围人数、局部密度、
  密度等级、压力和事件影响；
- `crowdsim_overlay_server.py` 的 `resolve_agent_decisions()` 挑选到期行人并调用
  `self.decision_engine.decide(agent)`；
- `resolve_agent_decisions()` 当前要求决策引擎返回
  `(action, reason, source)` 三元组；
- `_advance_agents()` 已根据 `agent.decision` 调整速度，并在部分情况下触发绕行；
- `websocket_server.py` 在每次 `simulator.step()` 后调用
  `resolve_agent_decisions()`，随后才把当前帧发送给前端。

因此推荐做法是新增一个很薄的决策引擎适配器，并用它替换当前
`AgentDecisionEngine`。不要在逐帧循环中再并行增加第二套大模型调用，否则会
重复决策和重复计费。

## 3. 环境与配置

安装依赖：

```bash
conda activate sumo
python -m pip install -r pedestrian_decision_skill/requirements.txt
```

DeepSeek 本地明文配置位于：

```text
pedestrian_decision_skill/config.json
```

示例：

```json
{
  "api_key": "填写真实 DeepSeek API Key",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-v4-flash",
  "timeout_seconds": 10,
  "max_tokens": 160,
  "temperature": 0.1
}
```

`config.json` 已在仓库根目录 `.gitignore` 中忽略。不要把 API Key 写入环境诊断、
异常日志、前端数据或 WebSocket 消息，也不要提交真实 Key。可提交的结构模板是
`config.example.json`。

## 4. Skill 输入协议

适配器向 `decide()` 传入普通字典：

```json
{
  "agent_id": "p-1024",
  "profile": {
    "nationality": "unknown",
    "language": "zh"
  },
  "current_state": {
    "status": "continue",
    "speed": 1.1,
    "stress": 0.35,
    "fatigue": 0.2,
    "flood_impact": 0.1,
    "event_impact": 0.25
  },
  "surrounding_crowd": {
    "nearby_people": 12,
    "local_density": 1.8,
    "density_level": "busy"
  }
}
```

Skill 会补齐缺失字段、转换可解析的数字、限制数值范围并过滤协议外字段。尽管
存在整理器，适配器仍应优先传入语义正确的数据。

## 5. 当前 Agent 字段映射

| Skill 字段 | 当前后端来源 | 状态 | 说明 |
|---|---|---|---|
| `agent_id` | `agent.id` | 可直接映射 | Skill 会转换为字符串 |
| `profile.nationality` | 暂无 | 待确认 | 当前只有 `cultural_group`，不能默认把文化组当作国籍 |
| `profile.language` | `agent.native_language` | 可直接映射 | 缺失时使用 `unknown` |
| `current_state.status` | `agent.decision` | 临时映射 | 表示当前/上次高层动作；如后续增加正式行为状态字段，应替换 |
| `current_state.speed` | `agent.speed` | 可直接映射 | 当前推进速度，代码语义为非负速度 |
| `current_state.stress` | `agent.stress` | 可直接映射 | `crowd_environment.py` 已限制在 `0..1` |
| `current_state.fatigue` | `agent.fatigue` | 可直接映射 | `_advance_agents()` 已限制在 `0..1` |
| `current_state.flood_impact` | `agent.flood_impact` | 可直接映射 | 当前路段积水影响 |
| `current_state.event_impact` | `agent.event_impact` | 可直接映射 | 当前事件空间影响，范围 `0..1` |
| `surrounding_crowd.nearby_people` | `agent.nearby_people` | 可直接映射 | 同一路段、感知半径内其他行人组规模之和 |
| `surrounding_crowd.local_density` | `agent.local_density` | 可直接映射 | 当前实现包含事件压力加权，不是未经修正的纯物理密度 |
| `surrounding_crowd.density_level` | `agent.density_level` | 可直接映射 | `free/busy/crowded/critical` |

当前 `nearby_people` 使用每个行人的 `perception_radius`，只统计同一路段且纵向
距离不超过该半径的其他行人，并按对方 `group_size` 求和。

当前局部密度近似按以下方式计算：

```text
area = 2 × perception_radius × max(1, segment.width)
local_density = (nearby_people + group_size) / area × event_pressure_factor
```

因此，在论文、接口说明或阈值标定中使用 `local_density` 前，需要明确它是后端的
事件加权局部指标。

### 必须由适配负责人确认的字段

1. 是否会在 `Agent` 上增加真正的 `nationality` 字段；在此之前传 `unknown`。
2. 是否新增正式的行为状态字段；在此之前可把 `agent.decision` 作为 `status`。
3. `nationality` 采用国家名、ISO 国家码还是项目自定义枚举。
4. `language` 是否继续使用 `native_language` 的 `zh/en/other` 取值。
5. 是否需要把 `age_group` 加入未来 v2 协议；v1 Skill 当前不会接收年龄。

## 6. 最小适配器示例

下面的代码展示接口形状，不会由 Skill 自动写入后端。建议放到独立模块，例如
`skill_decision_adapter.py`，具体命名由适配负责人决定。

```python
from typing import Any, Dict, Tuple

from crowdsim_models import Agent
from pedestrian_decision_skill import PedestrianDecisionSkill


class SkillDecisionEngineAdapter:
    def __init__(self) -> None:
        self.skill = PedestrianDecisionSkill()
        self.llm_decisions = 0
        self.fallback_decisions = 0

    @staticmethod
    def build_context(agent: Agent) -> Dict[str, Any]:
        return {
            "agent_id": agent.id,
            "profile": {
                # 当前 Agent 没有真实国籍字段，不能用 cultural_group 冒充。
                "nationality": getattr(agent, "nationality", "unknown"),
                "language": getattr(agent, "native_language", "unknown"),
            },
            "current_state": {
                "status": getattr(agent, "decision", "unknown"),
                "speed": agent.speed,
                "stress": agent.stress,
                "fatigue": agent.fatigue,
                "flood_impact": agent.flood_impact,
                "event_impact": agent.event_impact,
            },
            "surrounding_crowd": {
                "nearby_people": agent.nearby_people,
                "local_density": agent.local_density,
                "density_level": agent.density_level,
            },
        }

    async def decide(self, agent: Agent) -> Tuple[str, str, str]:
        result = await self.skill.decide(self.build_context(agent))
        if result["source"] == "llm":
            self.llm_decisions += 1
        else:
            self.fallback_decisions += 1
        return result["action"], result["reason"], result["source"]

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "backend": "pedestrian_decision_skill",
            "llm_decisions": self.llm_decisions,
            "fallback_decisions": self.fallback_decisions,
        }
```

该适配器保留了 `resolve_agent_decisions()` 当前需要的三元组接口，也提供了
`OverlayServer._send_init()` 和 `frame()` 当前会调用的 `diagnostics()`。

在 `crowdsim_overlay_server.py` 中，最小替换点是：

```python
# 原实现
from agent_decision import AgentDecisionEngine
self.decision_engine = AgentDecisionEngine()

# 适配后（模块名仅为示例）
from skill_decision_adapter import SkillDecisionEngineAdapter
self.decision_engine = SkillDecisionEngineAdapter()
```

不要同时让旧 `AgentDecisionEngine` 和新 Skill 为同一行人发起模型请求。旧引擎的
`CROWDSIM_LLM_*` 环境变量配置与 Skill 的 `config.json` 是两套配置；完成替换后，
应明确哪一套被停用。

## 7. Skill 输出协议

```python
{
    "action": "avoid",
    "reason": "局部密度较高且事件影响明显",
    "confidence": 0.86,
    "source": "llm",
}
```

| 字段 | 取值 | 适配器用途 |
|---|---|---|
| `action` | 五个固定动作之一 | 写入 `agent.decision` |
| `reason` | 非空且不超过 80 字符 | 写入 `agent.decision_reason` |
| `confidence` | `0..1` | 当前 Agent 无对应字段；最小适配可忽略，或后续增加字段 |
| `source` | `llm/local_fallback` | 写入 `agent.decision_source` 并统计诊断指标 |

配置无效、请求失败、DeepSeek 响应异常或模型决策未通过协议校验时，`decide()`
自动返回 `source="local_fallback"`。最外层输入不是字典及未知程序异常不会被
Skill 隐藏。

## 8. 五种动作在当前后端中的实际效果

当前 `_advance_agents()` 已识别全部五个动作：

| 动作 | 当前速度因子 | 当前附加行为 | 接入判断 |
|---|---:|---|---|
| `continue` | `1.0` | 保持当前路线 | 已有基础语义 |
| `slow_down` | `0.62` | 无额外路线操作 | 已有基础语义 |
| `avoid` | `0.82` | 进入下一道路时可强制 `_maybe_divert()` | 已有基础避让，但不是立即重规划 |
| `follow_crowd` | `0.9` | 当前没有真实的人群方向跟随算法 | 目前实质主要是速度调整，需确认是否接受 |
| `wait` | `0.08` | 仍受行人最小速度约束 | 当前不是完全静止，需确认是否接受 |

需要特别注意：

- `avoid` 不会立刻改变当前位置或目的地，只会影响速度，并可能在进入新道路时
  调用已有绕行逻辑；
- `follow_crowd` 当前没有根据邻居方向改变路线；
- `wait` 仍可能被 `_advance_agents()` 的最小速度限制抬高到非零速度；
- Skill 不返回目标速度、持续时间、新道路或新路线，这些语义属于后端适配层。

如果当前动作语义不满足仿真需求，应先在后端明确动作实现，再修改 Prompt；不要
让模型返回后端无法执行的参数。

## 9. 调度、并发与仿真节奏

当前后端已经避免“每个行人每一帧调用模型”：

- `step_length = 0.5` 秒；
- `decision_interval_steps = 10`，普通行人约每 5 个仿真秒到期一次；
- 危险密度行人的下次决策间隔为 4 步，即约 2 个仿真秒；
- `max_llm_decisions_per_step = 12`；
- 到期行人按危险密度和压力排序；
- 同一批决策通过 `asyncio.gather()` 并发执行。

这些限制可以复用，但接入负责人必须评估 API 额度、并发限制和实时体验。当前
WebSocket 循环会等待整批 `resolve_agent_decisions()` 完成后才发送下一帧，因此
最慢请求可能让画面停顿，最长接近配置的超时时间。

基础接入建议：

1. 首次联调时把 `max_llm_decisions_per_step` 降到 `1..3`；
2. 保留每个行人的 `next_decision_step`，不要逐帧请求；
3. 记录一批决策的总耗时与 `source` 比例；
4. 压力测试确认稳定后再逐步提高并发；
5. 若改为后台任务，应用结果前重新检查 `agent.id` 是否仍存在，并检查结果是否
   已经过期；
6. 同一行人只能有一个未完成决策，避免旧结果覆盖新状态。

## 10. 建议记录的诊断信息

可以记录：

- 仿真步、`agent_id` 和决策耗时；
- `action`、`confidence` 和 `source`；
- 累计 `llm_decisions` 与 `fallback_decisions`；
- 聚合后的请求失败类型和校验失败次数。

不要记录：

- API Key 或 Authorization 请求头；
- 完整 `config.json`；
- 包含密钥的异常上下文；
- 无限制增长的完整 Prompt/响应日志。

当前 Skill 的兜底结果不会包含底层失败原因。需要排查真实 API 时，应使用严格的
实时测试脚本，而不是根据 `local_fallback` 猜测原因。

## 11. 验证流程

### 11.1 离线 Skill 测试

```bash
conda activate sumo
python -m unittest discover -s pedestrian_decision_skill/tests -v
```

这些测试使用模拟 HTTP，不消耗 DeepSeek 额度。

### 11.2 后端回归测试

```bash
MPLCONFIGDIR=/tmp/crowdsim-mpl-cache \
python -m unittest discover -s tests -v
```

### 11.3 单次真实 API 测试

```bash
python -m pedestrian_decision_skill.examples.live_deepseek_test --live
```

该脚本只发送一次请求，并且不启用本地兜底。成功结果的 `source` 应为 `llm`。

### 11.4 适配器验收

适配完成后至少验证：

1. 普通场景可以得到 `continue`；
2. 危险密度或高事件影响可以得到 `avoid`；
3. 拥挤或高压力可以得到 `slow_down`；
4. 临时使用错误 Key 时仿真仍继续运行，且结果为 `local_fallback`；
5. 行人完成路线并删除后，不会再应用迟到的决策；
6. 前端帧中的 `decision/decision_reason/decision_source` 与应用结果一致；
7. API Key 不出现在控制台、帧数据和仓库状态中。

## 12. 交接检查表

- [ ] `nationality` 的真实来源和枚举已确认；
- [ ] `status` 的正式语义已确认；
- [ ] `native_language` 到 `language` 的映射已确认；
- [ ] `local_density` 的事件加权语义已被接受；
- [ ] 五个动作在当前后端中的实际效果已确认；
- [ ] `follow_crowd` 是否需要真实方向跟随已确认；
- [ ] `wait` 是否需要完全停止已确认；
- [ ] 新旧决策引擎不会同时请求模型；
- [ ] 请求间隔和单步最大并发已确定；
- [ ] 过期结果和已离场行人的处理策略已确定；
- [ ] API Key 未被提交或输出；
- [ ] Skill 离线测试通过；
- [ ] 后端回归测试通过；
- [ ] 单次真实 API 测试通过；
- [ ] 适配后的仿真场景验收通过。
