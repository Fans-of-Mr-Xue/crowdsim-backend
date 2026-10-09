# CrowdSim SUMO Backend

该后端以 SUMO 1.24.0 striping 为唯一运动引擎。SUMO 管理行人实际位置、速度、道路交互、出发和到达；Python 管理画像、局部观察、心理状态、信息、策略、危险输入、实验记录和 WebSocket 协议。事件不会直接乘人数或密度。

## 工程结构

```text
crowdsim/
├─ domain/          数据合同、画像、同行关系
├─ core/            仿真生命周期、人口账本、状态更新、命令队列
├─ environment/     局部感知、事件、信息、危险、干预、POI
├─ decision/        规则/LLM接口、调度、路线候选、计划执行
├─ experiments/     C0—C5控制器、配对实验、统计报告和独立HTTP/SSE服务
└─ infrastructure/  SUMO/路网适配、WebSocket、帧、指标、实验记录

arde_emergency/     ARDE 状态、双层决策、奖励、动作映射与兼容接口

data_service/       独立 MongoDB 与知识库数据接口；connectors/、crawlers/ 预留气象连接器及采集任务

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
D:\Anaconda\python.exe -m pip install -e .
D:\sumo-1.24.0\bin\sumo.exe --version
```

`pip install -e .` 会安装仓库内的 `crowdsim` 与 `arde_emergency`。C0—C5 对比实验、
ARDE 算法和 SUMO 仿真全部由本仓库提供，运行时不依赖 MACE。

`traci`、`sumolib` 和 SUMO 二进制必须使用相同版本。仅安装 Python 包不包含完整 SUMO 仿真程序。

## 启动

日常使用只需在仓库根目录执行一个后端命令：

```sh
./run.sh
```

统一入口 `start_backend.py` 自动检查 SUMO 和依赖，复用或建立模型（本机 8800 → 服务器 8800）与数据库（本机 27018 → 服务器 27018）的 SSH 隧道，然后启动 SUMO（8765）、对话存储（8767）和数据服务（8768）。这些模块仍以独立进程运行，不会把爬虫或数据库操作加入仿真循环。前端仍用 `npm run dev` 单独启动，无需再手动启动对话服务或执行 SSH 映射命令。

首次配置时，复制 `.env.example` 为 `.env` 并填写 MongoDB 密码和 SSH 参数；已有 `.env` 按模板补齐 `CROWDSIM_MODEL_SSH_*` 即可。默认模板使用已知数据库 SSH 账号 `hyq`；如果模型服务器账号不同，请修改 `CROWDSIM_MODEL_SSH_USER`。远程模型服务需要已经运行并提供本机 8800 对应的 OpenAI 兼容接口，启动器负责建立连接，不负责在远端部署模型。

SSH 使用现有密钥或 ssh-agent，也可在终端按提示输入密码；密码不会写入配置。统一入口使用明确的 SSH 连接参数，不读取用户 SSH 配置中的其他转发规则。已有可用服务和连接会被复用；Ctrl+C 会停止本次创建的服务和隧道，复用的外部进程会保留。启动失败时会清理本次创建的进程，输出失败原因。

统一入口要求 Python 3.10 或更新版本。`run.sh` 优先使用 `CROWDSIM_PYTHON`，其次使用明确设置的 `CROWDSIM_CONDA` 环境，再使用本仓库 `.venv-backend/bin/python`，最后使用 PATH 中的 `python`。如需在其他电脑初始化独立环境，可执行：

```sh
python -m venv .venv-backend
.venv-backend/bin/python -m pip install -r requirements.txt
```

需要 SUMO 1.24.0 二进制程序；已经安装 SUMO 的电脑可设置 `SUMO_HOME` 或 `SUMO_BINARY`。也可按 [SUMO 官方安装说明](https://sumo.dlr.de/docs/Downloads.php#python_packages__virtual_environments) 在该环境中安装匹配版本的程序：

```sh
.venv-backend/bin/python -m pip install eclipse-sumo==1.24.0
```

也可以直接用自己的 Python 环境启动或只做环境检查：

```sh
python start_backend.py
./run.sh --check
```

默认使用热点场景，可通过 `./run.sh --scenario research` 切换。`--no-tunnels` 表示只复用现有数据库和模型连接，不创建 SSH 隧道。原有模块入口仍可单独使用，下面是分别启动时的说明。

共享 MongoDB 数据集接口、前端连接和独立数据库服务启动见[数据库接入说明](docs/database_api.md)。数据服务使用独立 8768 进程：

```sh
python -m pip install -r requirements.txt
python -m data_service --port 8768
```

本机开发时，前后端交互与数据库连接使用不同端口：

```text
前端（通过开发代理调用 HTTP 接口）
  → 本机 127.0.0.1:8768：Python 数据服务
  → 本机 127.0.0.1:27018：SSH 隧道入口
  → 服务器 127.0.0.1:27018：MongoDB
```

`8768` 是数据服务的 HTTP 监听端口；后端通过本机 `27018` 连接数据库，SSH 隧道再将连接转发到服务器的 `27018`。启动数据服务时须保持SSH端口映射隧道运行。
SUMO 的 8765 进程不加载数据服务；爬虫也应单独执行。

```powershell
$env:SUMO_HOME='D:\sumo-1.24.0'
D:\Anaconda\python.exe crowdsim_overlay_server.py --host 127.0.0.1 --port 8765
```

默认 WebSocket 地址为 `ws://127.0.0.1:8765`，事后阶段任务 HTTP API 同时挂在 `http://127.0.0.1:8765/api/v1/post`；只需启动这一个 8765 进程。WebSocket 协议见 `docs/websocket_protocol.md`，事后请求与响应契约见 [事后任务 API 规范](postanalysis_api/README.md)。启动前须安装更新后的 `requirements.txt`（新增 `aiohttp`，用于同一端口处理 HTTP 与 WebSocket）。

如需运行原有、独立的 C0—C5 控制实验，可另开终端启动其 8766 服务；**事后反事实工作台和新任务 API 不依赖它**：

```powershell
D:\Anaconda\python.exe -m crowdsim.experiments --host 127.0.0.1 --port 8766 --gateway-url ws://127.0.0.1:8765
```

新事后 API 位于 8765 的 `/api/v1/post/*`，原有 8766 `/crowdSim/control/*` 行为不变。当前批量 SUMO 执行器和场景区域映射尚未接入，创建草稿可用，启动真实批次会明确返回 `503 CAPABILITY_UNAVAILABLE`。

前端控制接口为 `http://127.0.0.1:8766/crowdSim/control`，运行事件使用同一服务的 SSE。
实验配置、逐次运行、观测、决策、动作回执和报告默认保存到
`runs/control_experiments/`，不需要 MongoDB。控制方法包括：

- C0：无调控观测基线；
- C1：固定阈值规则；
- C2：大模型单次决策；
- C3：大模型周期决策；
- C4：大模型事件触发决策；
- C5：ARDE 动态双层调控。

C2—C4 使用 OpenAI 兼容接口。正式运行前设置 `CROWDSIM_LLM_BASE_URL`、
`CROWDSIM_LLM_API_KEY` 和 `CROWDSIM_LLM_MODEL`。未提供密钥或模型调用失败时，
控制器会明确记录 `fallback` 并使用 C1 规则结果，报告不会把回退结果描述成真实大模型基线。
模型密钥只能由后端环境变量提供；实验接口拒绝接收或持久化 API Key、令牌与密码。

默认启动普通外滩研究场景。启动“上海人民英雄纪念塔有限聚集”热点场景时，使用场景预设，让 SUMO 配置与行人路线保持成对选择；该热点预设目前只包含热点访客，不包含背景行人：

```powershell
D:\Anaconda\python.exe crowdsim_overlay_server.py --scenario hotspot --host 127.0.0.1 --port 8765
```

热点预设默认生成 700 名访客，也支持通过 `configure.count` 在 0—10000 人范围内重建需求，
或使用已保存需求的 `population.total`（1—10000 人）；该场景不包含背景行人。需求界定详情、
`requirement_id` 及运行快照见 `docs/websocket_protocol.md`。后台按 240—660 s 的三段目标到达曲线
倒推每名访客的刷新时刻，活动在 600 s 开始、1000 s 结束，并在之后 120 s 内逐渐释放离场。
服务向初始化帧输出 0—1800 s 的热点观察时间轴，共 3600 个 0.5 s 步。

如需使用自定义 SUMO 配置，必须同时指定配置中实际引用的行人路线；已内置的两个配置可以省略 `--ped-routes`：

```powershell
D:\Anaconda\python.exe crowdsim_overlay_server.py --config scenarios\custom\scenario.sumocfg --ped-routes scenarios\custom\pedestrians.rou.xml
```

使用本地 `pedestrian_decision_skill/config.json` 中的 DeepSeek 配置启动 LLM 决策：

```powershell
D:\Anaconda\python.exe crowdsim_overlay_server.py --mode llm --host 127.0.0.1 --port 8765
```

## 场景预检

```powershell
D:\Anaconda\python.exe scripts\validate_scenario.py --all --duration 30 --output runs\stage1\scenario_validation.json
```

脚本会严格检查 striping、0.5 s 步长、路线、解堵阈值、行人 lane、真实 person 插入及 FCD 位移，并重建四类微型场景。

## 实验与回放

```powershell
D:\Anaconda\python.exe scripts\run_experiment.py --count 20 --steps 120 --mode rule
D:\Anaconda\python.exe scripts\run_experiment.py --count 20 --until-finished --mode rule
# 兼容旧内联日志与新拆表日志；报告写入新的回放运行目录
D:\Anaconda\python.exe scripts\replay_experiment.py runs\<source_run_id>
```

运行产物写入 `runs/<run_id>/`，包括 manifest、实际需求、画像、命令、消息、决策、生命周期、轨迹、指标、SUMO 日志和摘要。新决策日志默认使用六个 JSONL 表无损去重，不引入数据库；格式和测试见 [决策日志拆表](docs/decision_log_format.md)。读取器兼容旧格式与新格式，按需解析计划和路线；基础回放按时间批次消费计划与轨迹，不生成新规则或 LLM 决策，并比较位置、速度和人口账本。历史日志不会自动转换或改写。基础回放不支持包含已记录外部命令的实验，也不等于完整 context 复原或所有场景均可精确复现。

## DeepSeek 行人决策 Skill

背景行人 POI 重规划的可达性、合法位置、完整行程执行、重试冷却及诊断说明见 [POI 重规划补强](docs/poi_replanning.md)。功能保持开启，热点访客的锁定行程不被自主 POI 选择覆盖。

`pedestrian_decision_skill` 已实现可注入 `DecisionScheduler` 的 DeepSeek 决策引擎。它读取冻结的画像、状态、周围人群和有限候选，直接生成 `BehaviorPlan`；道路、速度和等待时间均由可信后端数据补齐。非法目标、超时或错误会记录并回退到规则计划。配置缺失时 `enabled=false`，不会把规则结果统计成真实 LLM 决策。详细接口见 `pedestrian_decision_skill/INTEGRATION.md`。

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
## 上海人民英雄纪念塔热点人群实验

默认实验现在使用“上海人民英雄纪念塔有限聚集”需求，只包含热点访客，不再生成背景行人。需求不是运行中补人，而是在启动前生成包含明确出发、到达、活动停留和离场阶段的 SUMO person。热点访客按三段目标到达曲线倒推出发时间，并按可用长度加权随机刷新到黄浦公园西侧的五条园内人行道路上。由于刷新位置已经属于公园路网，行人不再先折返南、北公园入口，而是直接在园内选择路线前往两个纪念塔入口；进入园内道路后允许根据画像和拥堵重新选择纪念塔入口。访客随后进入地面环道，按最佳、次优和普通观赏弧段的权重选择目标位置，停留到统一活动结束时刻后在 120 s 内逐渐离场，最后经公园道路离开至园外道路：

```powershell
D:\Anaconda\python.exe scripts\generate_hotspot_demand.py
D:\Anaconda\python.exe scripts\run_hotspot_experiment.py
D:\Anaconda\python.exe scripts\run_experiment.py --steps 3600
```

热点规模和时间窗见 `config/crowd_hotspots.json`。`runs/hotspot/people_heroes_monument_report.json` 记录环道人数、密度、入口低速行人、外围速度及后期消散。当前数值是涌现机制演示参数，未经过外滩实测标定。

在线帧把观测区拆成 `core`（外圈两条地面环道边）、`entries`（纪念塔两个入口）、`park`（黄浦公园内部道路）、`park_entries`（公园南北入口）和 `external_approach`（园外接近道路）。内侧地面环道和更深层环道不参与访客目标、路线或核心区测量。当前访客从 `park` 区域开始，`park_entries` 仍用于网络观测和离场过程，不再作为到访必经点。`core_process_state` 描述环道自身的 `normal → building → congested → dispersing → cleared`；安全口径的 `process_state` 还会检查园内、园外未完成访客与低速积压，必要时进入 `residual_congestion`，避免环道清空后误报整体消散。`visit_lifecycle` 给出未出发、园外接近/排队、园内接近/排队、进入环道、聚集停留、离场、完成和未完成人数。其中 `stopped_count` 表示计划停留，`slow_walking_count` 才表示步行阶段的低速人数。
