# 事后阶段本地任务 API：v1 契约与联调说明

本目录提供挂载在 SUMO 8765 服务内的 HTTP 接口骨架、后端字段校验、本地 JSON/JSONL 存储和真实结果计算入口。**当前没有 F-00/CF 批量 SUMO 执行器及 A/B/C、A/B 汇流区、安全区的路网/面积映射**。因此任务可以导入、创建和查询，但 `start` 返回 `503 CAPABILITY_UNAVAILABLE`；不会计时模拟任务、伪造完成次数或生成示例指标。SUMO 同事接入 `workers/executor.py` 的执行器并声明能力后，才允许真正启动。

## 进程、路径与目录

```text
postanalysis_api/
├─ api/                 datasets.py / simulations.py / experiments.py / results.py / model_connections.py
├─ schemas/             严格的请求校验、指标定义和版本
├─ services/            数据集、任务、指标、配对比较与 PC 分析
├─ workers/             可注入的真实 SUMO 批量执行器边界
├─ repositories/        本机 JSON 快照与追加式 JSONL 事件
└─ main.py              挂载到 SUMO 8765 服务的 HTTP 请求适配器，不单独监听端口
```

在 `Crowdbackend` 目录启动 SUMO 服务，事后 API 自动注册到同一端口：

```powershell
python crowdsim_overlay_server.py --host 127.0.0.1 --port 8765
```

同一个 8765 服务继续提供 `ws://127.0.0.1:8765/` 的原有实时仿真 WebSocket，并提供 `http://127.0.0.1:8765/api/v1/post` 的事后 HTTP 接口；不再为事后阶段启动 8766 进程。原有 C0—C5 控制服务如需使用，仍可独立运行于 8766，与新事后接口无关。前端仍在 **8080**，本地 vLLM 仍在 **8800**，8767 为对话服务。默认数据目录为 `Crowdbackend/runs/postanalysis/`，可用 `CROWDSIM_POST_DATA_DIR` 指定，无需 MongoDB。安装更新后的 `requirements.txt` 中的 `aiohttp` 后再启动 SUMO 服务；同一端口承载 HTTP 和 WebSocket 的方式见 [aiohttp 官方文档](https://docs.aiohttp.org/en/stable/web_quickstart.html)。

事后 HTTP 路径仅允许本机回环客户端访问；跨域仅允许 `http://localhost:8080` 和 `http://127.0.0.1:8080`。`X-CrowdSim-User` 和 `X-CrowdSim-Workspace` 都是必填、长度 1—64 的字母数字/下划线/短横线标识，资源按两者隔离。**这些浏览器可填写的标识不是身份认证**；如需从其他机器访问事后 HTTP 接口，须先补真正的登录鉴权与权限服务。

所有成功响应：`{"success":true,"data":...,"error":null}`。失败响应：`{"success":false,"data":null,"error":{"code":"...","message":"...","field":"...或null"}}`。`POST` 一律使用 `Content-Type: application/json`、UTF-8、最大 8 MiB，并携带 `Idempotency-Key`（1—128 字符，同一用户/工作区/方法/路径/键下，相同请求返回首次响应和 `Idempotency-Replayed: true`，不同请求返回 409）。服务在执行前持久化幂等占位；极端情况下进程中断在操作完成与响应封存之间，重试返回 `REQUEST_IN_PROGRESS`，需核对本地记录后用新键继续，避免静默重复执行。请求体任何层级不得带 `password`、`apiKey`、`secret`、`authorization`、`sshKey` 或以 `token` 结尾的字段。不要把 SSH 凭据放进实验方案。

## 能力与资源接口

| 方法与路径（前缀省略） | 作用 | 当前状态 |
| --- | --- | --- |
| `GET /capabilities` | 契约版本、执行器场景/模型/区域/动作/指标/并行上限、规则与指标目录 | 可用；当前 `simulation.ready=false` |
| `GET /model-connections` | 只读检查 `http://127.0.0.1:8800/v1/models`，返回配置模型是否可见 | 可用；无密钥时不返回密钥 |
| `POST /datasets`、`GET /datasets`、`GET /datasets/{id}` | 导入标准化时间线、列表、详情 | 可用 |
| `GET /datasets/{id}/timeline?fromSeconds=0&toSeconds=15600&regionId=A&offset=0&limit=500` | 按秒、区域分页读取；区间含两端 | 可用 |
| `POST /simulations`、`GET /simulations`、`GET /simulations/{id}` | 创建/查询一次事实仿真草稿 | 可用 |
| `POST /simulations/{id}/start`、`POST /simulations/{id}/cancel`、`GET /simulations/{id}/events?after=0` | 启动、取消、按游标查进度事件 | 启动等执行器 |
| `POST /experiments`、`GET /experiments`、`GET /experiments/{id}` | 创建/查询一批 F-00 + CF 方案 | 可用 |
| `POST /experiments/{id}/start`、`POST /experiments/{id}/cancel` | 启动/取消整批 | 启动等执行器 |
| `GET /experiments/{id}/events?after=0`、`GET /experiments/{id}/runs` | 任务事件和完整运行级记录 | 可用；未运行时为空 |
| `GET /results/{experimentId}/{section}` | `section` 为 `metrics`、`comparison`、`causal-graph`、`observation`、`intervention`、`mechanism` | 真实结果封存后可读；此前 409 |

`GET /model-connections` 的 `status` 为 `ready`、`model_unavailable`、`http_error`（另返回 `httpStatus`）或 `unreachable`；该端点不代替对话服务，也不改变 8800 的模型请求路径。期望模型 ID 由后端 `CROWDSIM_POST_LLM_MODEL` 设置，默认 `qwen3.8-flash-next`，页面显示名为 `Qwen3.8-Flash-Next`；可选 API Key 只从 `CROWDSIM_POST_LLM_API_KEY` 环境变量读取。

## 数据集请求

`POST /datasets` 请求字段全部必填：

| 字段 | 类型、范围 | 含义 |
| --- | --- | --- |
| `name` | 字符串 1—120 | 案例名称 |
| `sourceKind` | `synthetic_reference` / `observed` | 细粒度 Markdown 应标为前者，仅供参考曲线与校准 |
| `sourceNote` | 字符串 1—500 | 来源、生成方法或观测说明 |
| `observations` | 数组 1—50000 | 标准化时间线；同一区域同一秒不可重复 |
| `observations[].timeSeconds` | 整数 0—86400 | 从 20:00 起的仿真秒数；可跨午夜 |
| `observations[].regionId` | 字符串 1—64，字母数字/`_`/`-` | A/B/C 等区域标识 |
| `observations[].population` | 整数 0—1000000 | 分区在场人数；A/B/C 若互不重叠才可求和 |
| `observations[].density` | 数值或 `null`，0—1000 | 人/m²；缺失用 `null` |
| `observations[].meanSpeed` | 数值或 `null`，0—30 | m/s |
| `observations[].pressureProxy` | 数值或 `null`，0—1e9 | 已给出的参考值，不作为 SUMO 真值 |

导入后返回 `id`、SHA-256 `version`、`observationCount`。原始 Markdown 需由导入适配器解析为上述字段，前端不得把自由文本或磁盘路径直接当作可执行仿真输入。后端仍重新验证标准化数据。`sourceKind=synthetic_reference` 的数据只作参考曲线/校准目标，**不能代替 20:00 的 SUMO 初态，也不能作为 PC 的独立运行样本**。

## 事实仿真与反事实批次请求

`POST /simulations`：`datasetId`、`scenario`、`seed`、`metricIds` 均必填。`seed` 是 0—2147483647 的整数；`metricIds` 为 1—8 个不重复的指标 ID。

`POST /experiments`：

| 字段 | 类型、范围 / 必填约束 | 含义 |
| --- | --- | --- |
| `name` | 字符串 1—120，必填 | 批次名 |
| `datasetId` | 已存在且同工作区的 ID，必填 | 锁定数据集版本 |
| `scenario` | 对象，必填 | 见下一表；F-00 与所有组共用 |
| `seedStart` | 整数 0—2147483647，必填 | 第 `i` 次重复的种子为 `seedStart+i`；最大值不得溢出 |
| `parallelism` | 整数 1—8，必填 | 请求并行度，启动时还须 ≤ 执行器能力上限 |
| `metricIds` | 指标 ID 数组 1—8，无重复，必填 | 必须在能力目录中；请求的 PC 特征指标也必须在其中 |
| `pcFeatures` | 数组 2—20，必填 | 每次完整运行形成一行；各特征形成数值列 |
| `groups` | 数组 1—12，必填 | 仅 CF-01 等干预组，F-00 由后端自动生成 |

`scenario`：

| 字段 | 类型、范围 |
| --- | --- |
| `scenarioId`、`modelId` | 字符串 1—64，启动时必须由 SUMO 能力接口声明 |
| `modelVersion`、`geometryVersion`、`ruleVersion` | 非空字符串 ≤80；用于复现与结果一致性检查 |
| `startClock` | **只允许 `20:00`**；首版从相同模型初态完整重跑 |
| `durationSeconds` | 整数 2—86400；干预时间必须在 `(0,durationSeconds)` 内 |

`pcFeatures[]`：`id`（唯一字符串 ≤64）、`metricId`（已请求指标）、`regionId`（区域或 `global`）、`windowStartSeconds`（整数 ≥0）、`windowEndSeconds`（整数，严格晚于起点且不超过总时长）、`aggregation`（`peak`/`mean`/`end`）全部必填。**不同时间窗使用不同列**，例如 23:35 汇流区压力和 23:45 出口压力；不得将同一轨迹的时间步当独立样本。区域必须在执行器能力中有路网与有效面积映射。

`groups[]`：

| 字段 | 类型、范围 / 必填约束 |
| --- | --- |
| `id` | 唯一 `CF-01` 等两位数字 ID |
| `name` | 非空字符串 ≤120 |
| `repeatCount` | **每组独立**整数 1—50 |
| `hypothesis` | 非空字符串 ≤500 |
| `manipulatedFeatureId` | `pcFeatures[].id` 中的一个；声明希望检验的机制变量 |
| `interventions` | 1—10 个对象，按 `atSeconds` 升序；不得改变锁定的基线或仿真模型 |

`interventions[]`：`id`（同组唯一）、`actionType`（执行器声明的动作）、`targetRegionId`（执行器声明的区域）、`atSeconds`（整数 `1..durationSeconds-1`）、`parameters`（最多 20 项，值为有限数字或 ≤200 字符字符串）全部必填。动作参数的**动作专属范围**须由 SUMO 同事在能力接口的 `actionSchemas` 中公布（数字/整数上下界、枚举值、必填性），后端在启动时逐项核验；区域须在 `regionMap` 中有固定网格 ID 与有效面积；模型、几何、规则版本也要匹配能力接口。当前骨架不假定广播、限流等动作已经可执行。

后端计划 `max(groups[].repeatCount)` 次去重 F-00 基线，以及 `sum(groups[].repeatCount)` 次干预运行。各组使用同一共享种子序列的前缀：例如 A 组 10 次、B 组 20 次，需 20 次 F-00、30 次干预；每个干预运行以同一种子配对 F-00，基线种子不能重复算作两个 PC 样本。`progress` 分别返回计划、完成、失败及每组计数；前端不能由时间百分比推算完成次数。

下面是创建草稿的结构示例。`datasetId` 必须先通过导入接口取得；`scenarioId`、动作及区域只有在能力接口声明后才能启动：

```json
{
  "name": "出口分流反事实批次",
  "datasetId": "ds_实际导入后返回的ID",
  "scenario": {
    "scenarioId": "shanghai_memorial_tower",
    "modelId": "sumo_pedestrian",
    "modelVersion": "待SUMO同事提供",
    "geometryVersion": "待SUMO同事提供",
    "ruleVersion": "rule-model/1.0",
    "startClock": "20:00",
    "durationSeconds": 18000
  },
  "seedStart": 12000,
  "parallelism": 2,
  "metricIds": ["peak_density", "mean_speed", "peak_pressure_proxy"],
  "pcFeatures": [
    {"id": "junction_early_pressure", "metricId": "peak_pressure_proxy", "regionId": "AB_junction", "windowStartSeconds": 12900, "windowEndSeconds": 13200, "aggregation": "peak"},
    {"id": "exit_later_density", "metricId": "peak_density", "regionId": "A", "windowStartSeconds": 13500, "windowEndSeconds": 13800, "aggregation": "peak"}
  ],
  "groups": [
    {
      "id": "CF-01", "name": "提前分流", "repeatCount": 10,
      "hypothesis": "提前分流降低后续出口峰值密度",
      "manipulatedFeatureId": "junction_early_pressure",
      "interventions": [
        {"id": "divert-1", "actionType": "reroute_group", "targetRegionId": "AB_junction",
         "atSeconds": 12600, "parameters": {"share": 0.2}}
      ]
    }
  ]
}
```

## 任务状态、权限与重复提交

`draft → queued → running → completed/partial/failed`；`draft → cancelled`；`queued/running → cancelling → cancelled/failed`。服务重启时未结束的本地任务标为 `interrupted`，不会声称它们已完成。`start` 仅接受 `draft`，`cancel` 仅接受 `draft/queued/running`；无执行器、未映射区域、无模型或无所需指标时，`start` 不进入 `running`。同一实验内容如需再次运行，应创建新的批次 ID。相同幂等键重放创建/启动/取消请求时，不会再执行一次操作。

`GET /experiments/{id}` 返回锁定方案和版本、状态及真实进度；`GET /experiments/{id}/events` 返回追加式事件和 `nextAfter`。每次真实运行保存 `experimentId`、`planId`、`seed`、状态、模型/几何/规则/指标版本、原始轨迹与事件引用、运行级指标，以及 PC 特征列。结果读取仅允许同一 `ownerId` 与 `workspaceId`；越权返回 403，不存在返回 404。

主要响应字段与类型：

| 对象 | 必返字段与类型 | 约束 |
| --- | --- | --- |
| 数据集元信息 | `id:string`、`version:string`、`name:string`、`sourceKind:string`、`observationCount:integer`、`createdAt:string` | 时间线通过分页接口单独读取；版本为数据行 SHA-256 |
| 仿真任务 | `id:string`、`status:string`、`datasetVersion:string`、`scenario:object`、`seed:integer`、`progress:object`、`resultId:string|null` | `progress.planned=1`；完成数和失败数均为整数 |
| 反事实批次 | `id:string`、`status:string`、`datasetVersion:string`、`seedStart:integer`、`groups:array`、`progress:object`、`resultId:string|null` | `progress` 含 `baselinePlanned/baselineCompleted/interventionPlanned/interventionCompleted/failed` 及每组 `groupId/planned/completed/failed`；计数不超过计划 |
| 运行记录 | `id:string`、`experimentId:string`、`planId:string`、`seed:integer`、`status:string`、`metrics:object`、`features:object` | `planId=F-00` 或已锁定组 ID；同一 `(experimentId,planId,seed)` 不得重复；成功记录须有原始证据引用 |
| 事件页 | `events:array`、`nextAfter:integer` | 每条含 `sequence:integer`、`at:UTC ISO-8601 string`、`type:string`、`detail:object`；按 sequence 递增 |
| 指标值 | `value:number|null`、`unit:string`、`definitionVersion:string`、`missingReason:string|null` | `value=null` 时必须给出缺失原因；分组均值另给 `sampleCount/plannedCount` |
| 配对比较 | `groupId:string`、`baselinePlanId="F-00"`、`effects:object` | 每项含 `effect:number|null`、`ci95:[number,number]|null`、`unit`、`definitionVersion`、`validPairs/plannedPairs`、`missingReason` |
| PC 候选图 | `status:string`、`sampleCount:integer`、`graph:object` 或 `reason:string` | 图含骨架、未定向/已定向边、分离集、独立性检验记录与冲突；样本不足不返回伪图 |

进度事件只能由执行器依据真实运行回执更新。每组 `completed + failed ≤ planned`；最终 `completed` 必须关联同版本结果，部分失败用 `partial` 并保留缺失原因。执行器须为 SUMO、需求生成、Agent 决策及可能的模型采样分别记录随机种子或可复现设置；只给 SUMO 设种子不足以保证完整实验可复现。

## 指标、风险等级与分析结果

规则版本 `rule-model/1.0`，指标版本 `post-metrics/1.0`。5 m × 5 m 网格；密度由网格人数/有效面积计算。四级**压力风险区域**：低 `<1.0`、中 `[1.0,2.0)`、中高 `[2.0,3.5)`、高 `≥3.5` 人/m²。聚集事件为同一网格密度 `≥2.0` 持续 10 秒；拥挤事件为密度 `≥3.5` 且平均速度 `≤0.5 m/s` 同时持续 10 秒。高风险区域和拥挤事件是不同输出。广播、分流、替代路线的触发/解除值在 `GET /capabilities.metricCatalog.policyTriggers` 中版本化。

`peak_pressure_proxy = ρ × Var(v_x,v_y)` 为连续代理量，单位 `person/m2*(m/s)^2`，**不是 N 或接触压力**；其高低风险标签暂时沿用同位置的密度等级，不对代理量直接套用 1.0/2.0/3.5 阈值。`risk_exposed_unique` 为进入高风险网格的去重人数；`risk_exposure` 为高风险网格内人数乘仿真秒数求和。结果另附四级各自的去重人数和人·秒。`evacuation_time` 需要目标人员及安全区到达时间，未完成按右删失记 `null`；`resource_cost` 需要已版本化成本表。缺输入时不能补示例数字。

`services/metric_calculator.py` 接受执行器保存的完整轨迹、固定网格有效面积、人员 ID/二维速度、压力核观测点/半径、目标人员与安全到达、资源量/成本表，复用 `algorithm.metrics` 的密度、速度、压力和疏散公式。每项返回 `{"value":number|null,"unit":"...","definitionVersion":"post-metrics/1.0","missingReason":null|"..."}`。可用缺失原因包括 `NO_TRAJECTORY`、`AREA_UNMAPPED`、`NO_KERNEL_CONFIG`、`INSUFFICIENT_TIME_POINTS`、`NO_SAFE_ZONE_OR_CENSORED`、`NO_VERSIONED_COST_TABLE`、`MISSING_OR_FAILED_RUN`。时间积分使用每帧到下一帧的仿真秒数，不用墙上时钟。

`services/causal_analysis.py` 在**全部计划运行到终态**后启动：每个完整 F-00 种子运行形成一行，只取有限且同列的数值；F-00 样本不足、列常数或独立性检验失败时返回 `insufficient_data` 与原因。经典 Gaussian PC 只发现 F-00 的候选图，不混合不同干预环境。每组各自与同种子 F-00 计算配对差、95% 自助抽样区间、有效配对数；失败配对明确标为 `MISSING_OR_FAILED_PAIR`。只有候选边、时间先后、隔离干预证据和效应区间同时支持时，才给出 `interventionSupportedDirections`；其余边仍未定向。这个方向证据只针对仿真模型，不能直接称为真实世界已证明因果。

`observation` 返回 F-00 候选图，`intervention` 返回各组配对效应及方向证据，`mechanism` 需 SUMO 同事补充可切换规则、命令/消息/改道/到达等事件证据后才开放；未有证据时返回 409 `ANALYSIS_UNAVAILABLE`。分析结论文案由前端或 LLM 基于这些结构化证据生成，提示词必须约束：引用 run ID、组 ID、指标定义版本、有效样本数和缺失原因；区分合成参考曲线、真实 SUMO 运行和推断；不得补造缺失指标、因果方向或机理事件。

## 错误码

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| 400 | `INVALID_JSON`、`INVALID_QUERY` | JSON/查询参数错误 |
| 403 | `RESOURCE_FORBIDDEN` | 用户与工作区不匹配 |
| 405 | `METHOD_NOT_ALLOWED` | HTTP 方法不受支持 |
| 404 | `NOT_FOUND` | 资源或路径不存在 |
| 409 | `IDEMPOTENCY_CONFLICT`、`REQUEST_IN_PROGRESS`、`INVALID_TASK_STATE`、`DATASET_VERSION_MISMATCH`、`RESULT_VERSION_MISMATCH` | 幂等键冲突、状态/版本冲突 |
| 409 | `RESULT_NOT_READY`、`ANALYSIS_UNAVAILABLE`、`RUN_SET_MISMATCH`、`TASK_NOT_TERMINAL`、`DUPLICATE_RUN` | 结果未形成或运行集合不一致 |
| 413 | `PAYLOAD_TOO_LARGE` | 请求体超过 8 MiB |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | 非 JSON 请求体 |
| 422 | `VALIDATION_ERROR`、`SECRET_FIELD_FORBIDDEN`、`TIME_ORDER_INVALID`、`SEED_RANGE_EXCEEDED` | 字段、时间或种子无效 |
| 422 | `SCENARIO_UNSUPPORTED`、`MODEL_UNSUPPORTED`、`MODEL_VERSION_MISMATCH`、`GEOMETRY_VERSION_MISMATCH`、`RULE_VERSION_MISMATCH` | SUMO 场景、模型或版本不匹配 |
| 422 | `ACTION_UNSUPPORTED`、`ACTION_SCHEMA_MISSING`、`ACTION_PARAMETER_INVALID`、`REGION_UNMAPPED`、`METRIC_UNSUPPORTED`、`METRIC_CAPABILITY_MISMATCH`、`RESOURCE_LIMIT_EXCEEDED` | 动作、区域、指标或资源能力不满足 |
| 503 | `CAPABILITY_UNAVAILABLE` | 批量执行器尚未接入 |
| 500 | `INTERNAL_ERROR` | 服务内部错误；堆栈仅写后端日志 |

## 前端校验与待接工作

前端应先读取 8765 的 `GET /capabilities`，按返回上限校验每组 `repeatCount`、总组数、并行度、区域/动作/指标能力；检查必填项、严格数值类型、`atSeconds` 时间顺序和 PC 特征窗口；显示后端 `error.field` 与 `error.code`。这些校验是交互提示，最终约束仍由后端执行。反事实工作台的原有 WebSocket 连接已改为 8765；页面布局和交互未调整。该页面当前仍发送旧式 `start(mode=counterfactual_batch)`，其载荷不含完整数据集、干预参数和逐组次数，8765 会明确返回 `batch_adapter_unavailable`，不会把一次实时仿真误当成批量实验。真实联调时还需让页面通过同一 8765 的 HTTP 接口提交完整方案，并改为逐组次数、真实进度和结果，停止使用演示公式结果。

SUMO 同事需提供：A/B/C、A/B 汇流区、安全区及 5 m 网格的有效面积/路网 ID；20:00 独立初态与固定种子重跑；可执行干预动作及参数边界；独立进程或其他安全并行方式；每个运行的轨迹、人员 ID/速度、到达和事件日志；模型/几何/规则版本；取消回执和资源上限。接入前 `GET /capabilities.simulation.ready=false` 是正确状态。
