# CrowdSim SUMO Backend

该后端以 SUMO 1.24.0 striping 为唯一运动引擎。SUMO 管理行人实际位置、速度、道路交互、出发和到达；Python 管理画像、局部观察、心理状态、信息、策略、危险输入、实验记录和 WebSocket 协议。事件不会直接乘人数或密度。

## 工程结构

```text
crowdsim/
├─ domain/          数据合同、画像、同行关系
├─ core/            仿真生命周期、人口账本、状态更新、命令队列
├─ environment/     局部感知、事件、信息、危险、干预、POI
├─ decision/        规则/LLM接口、调度、路线候选、计划执行
└─ infrastructure/  SUMO/路网适配、WebSocket、帧、指标、实验记录

config/             参数、属性注册表和POI
scenarios/          正式外滩SUMO场景
scripts/            运行、回放和验收工具
tests/              自动测试及仅供测试的SUMO场景
docs/               设计、协议、论文依据和实施记录
runs/               不纳入版本控制的实验产物
```

根目录只保留兼容启动入口 `crowdsim_overlay_server.py`。完整模块职责见 `docs/工程目录与模块边界.md`。

## 环境

- Python 3.12：`D:\Anaconda\python.exe`
- SUMO 1.24.0：设置 `SUMO_HOME`，本机当前为 `D:\sumo-1.24.0`

```powershell
D:\Anaconda\python.exe -m pip install -r requirements.txt
D:\sumo-1.24.0\bin\sumo.exe --version
```

`traci`、`sumolib` 和 SUMO 二进制必须使用相同版本。仅安装 Python 包不包含完整 SUMO 仿真程序。

## 启动

```powershell
$env:SUMO_HOME='D:\sumo-1.24.0'
D:\Anaconda\python.exe crowdsim_overlay_server.py --host 127.0.0.1 --port 8765
```

默认地址为 `ws://127.0.0.1:8765`。协议见 `docs/websocket_protocol.md`。

## 场景预检

```powershell
D:\Anaconda\python.exe scripts\validate_scenario.py --all --duration 30 --output runs\stage1\scenario_validation.json
```

脚本会严格检查 striping、0.5 s 步长、路线、解堵阈值、行人 lane、真实 person 插入及 FCD 位移，并重建四类微型场景。

## 实验与回放

```powershell
D:\Anaconda\python.exe scripts\run_experiment.py --count 20 --steps 120 --mode rule
D:\Anaconda\python.exe scripts\run_experiment.py --count 20 --until-finished --mode rule
D:\Anaconda\python.exe scripts\replay_experiment.py runs\<run_id>
```

运行产物写入 `runs/<run_id>/`，包括 manifest、实际需求、画像、命令、消息、决策、生命周期、轨迹、指标、SUMO 日志和摘要。回放只使用已保存计划，不调用 LLM，并比较位置、速度和人口账本。

## LLM 预留接口

当前不绑定模型厂商或 HTTP 协议。接入点是 `crowdsim/decision/llm_gateway.py:LlmGateway.complete(context)`；应用层提供实现并注入 `AgentDecisionEngine` 后，模型只能从后端给出的有限候选中选择。非法目标、超时或错误会明确记录，并使用同一 `BehaviorPlan` 合同的规则回退。在具体接入方法尚未提供前，`--mode llm` 会明确退回规则模式，验收也不会伪报真实模型已联通。

## 测试与验收

```powershell
D:\Anaconda\python.exe -m pytest -q
D:\Anaconda\python.exe scripts\run_acceptance.py
```

`run_acceptance.py` 为 F01—F18 生成逐项状态。缺少真实模型凭据或实际前端联调时，对应项目保持 `partial`，不会把 skip 或模拟返回写成完整通过。

## 系统循环

```text
S(t)冻结快照
  → 事件/消息在边界生效
  → 局部观察与到期决策
  → 规则或LLM生成BehaviorPlan
  → 校验并通过TraCI执行
  → SUMO推进唯一一个0.5s步长
  → 批量更新压力/疲劳与人口账本
  → 计算真实密度/速度/流量并发送S(t+dt)
```

明确不支持运行时任意多边形硬障碍、动态硬封路、火灾/积水物理场、身体接触压力或连续二维群体队形；相关请求必须明确拒绝。
