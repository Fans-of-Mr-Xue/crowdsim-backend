# 事后阶段本地任务 API：v1 契约与联调说明

当前执行与分析字段以[事后共同契约](../docs/post_execution_analysis_contract.md)为准；该文件按真实workers执行路径核对，并由事后设计对话加载。待扩展的SUMO/TraCI、测量和后端工作见[对接清单](../docs/post_sumo_handoff.md)，不能视为已经接入。

本目录提供挂载在 SUMO 8765 服务内的 HTTP 接口、字段校验、MongoDB 持久化及真实结果计算。现已接入 `workers/sumo_batch.py`，使用独立SUMO进程执行纪念塔 `memorial-post-test` 的F-00/CF批次，保留现有事中运行时。A→M1、B→M2、C→M3为已确认工程测试映射，M3不是真实北侧通道；其他场景仍须声明能力后才能执行。源数据未观测指标保持null，实际指标来自FCD/tripinfo，未校准误差单独展示。

事后数据集、方案、任务及API结果保留在 `postanalysis_*` 集合，上传原件保存在 `postanalysis_files` GridFS bucket。新增 `post` 总索引及每次运行以UTC时间戳和类型命名的明细、轨迹、结果集合。完整规范数据、执行矩阵、页面流程、算法定义及验证命令见[纪念塔事后完整测试](../docs/post_memorial_end_to_end_test.md)。

## 进程、路径与目录

```text
postanalysis_api/
├─ api/                 datasets.py / simulations.py / experiments.py / results.py / model_connections.py
├─ schemas/             严格的请求校验、指标定义和版本
├─ services/            数据集、任务、指标、配对比较与 PC 分析
├─ workers/             可注入的真实 SUMO 批量执行器边界
├─ repositories/        MongoDB 文档、GridFS 原件与追加式事件
└─ main.py              挂载到 SUMO 8765 服务的 HTTP 请求适配器，不单独监听端口
```

在 `Crowdbackend` 目录启动 SUMO 服务，事后 API 自动注册到同一端口：

```powershell
python crowdsim_overlay_server.py --host 127.0.0.1 --port 8765
```

同一个 8765 服务继续提供 `ws://127.0.0.1:8765/` 的原有实时仿真 WebSocket，并提供 `http://127.0.0.1:8765/api/v1/post` 的事后 HTTP 接口；不再为事后阶段启动 8766 进程。原有 C0—C5 控制服务如需使用，仍可独立运行于 8766，与新事后接口无关。前端仍在 **8080**，本地 vLLM 仍在 **8800**，8767 为对话服务。启动前请按 `Crowdbackend/.env.example` 配置 MongoDB；连接变量与共享数据集服务一致，未连接 MongoDB 时数据写入接口会返回 `503 STORAGE_UNAVAILABLE`。安装更新后的 `requirements.txt` 中的 `aiohttp` 后再启动 SUMO 服务；同一端口承载 HTTP 和 WebSocket 的方式见 [aiohttp 官方文档](https://docs.aiohttp.org/en/stable/web_quickstart.html)。

事后 HTTP 路径仅允许本机回环客户端访问；跨域仅允许 `http://localhost:8080` 和 `http://127.0.0.1:8080`。`X-CrowdSim-User` 和 `X-CrowdSim-Workspace` 都是必填、长度 1—64 的字母数字/下划线/短横线标识，资源按两者隔离。**这些浏览器可填写的标识不是身份认证**；如需从其他机器访问事后 HTTP 接口，须先补真正的登录鉴权与权限服务。

所有成功响应：`{"success":true,"data":...,"error":null}`。失败响应：`{"success":false,"data":null,"error":{"code":"...","message":"...","field":"...或null"}}`。`POST` 一律使用 `Content-Type: application/json`、UTF-8、最大 16 MiB，并携带 `Idempotency-Key`（1—128 字符，同一用户/工作区/方法/路径/键下，相同请求返回首次响应和 `Idempotency-Replayed: true`，不同请求返回 409）。服务在执行前持久化幂等占位；极端情况下进程中断在操作完成与响应封存之间，重试返回 `REQUEST_IN_PROGRESS`，需核对 MongoDB 记录后用新键继续，避免静默重复执行。请求体任何层级不得带 `password`、`apiKey`、`secret`、`authorization`、`sshKey` 或以 `token` 结尾的字段。不要把 SSH 凭据放进实验方案。

## 能力与资源接口

| 方法与路径（前缀省略） | 作用 | 当前状态 |
| --- | --- | --- |
| `GET /capabilities` | 契约版本、执行器场景/模型/区域/动作/指标/并行上限、规则与指标目录 | 可用；本机SUMO存在时纪念塔测试场景ready |
| `GET /model-connections` | 只读检查 `http://127.0.0.1:8800/v1/models`，返回配置模型是否可见 | 可用；无密钥时不返回密钥 |
| `POST /datasets`、`GET /datasets`、`GET /datasets/{id}` | 导入标准化时间线、列表、详情 | 可用 |
| `POST /review-plans`、`GET /review-plans`、`GET /review-plans/{id}` | 保存和读取事实校准目标及完整事后方案草案 | 可用 |
| `GET /datasets/{id}/timeline?fromSeconds=0&toSeconds=15600&regionId=A&offset=0&limit=500` | 按秒、区域分页读取；区间含两端 | 可用 |
| `POST /simulations`、`GET /simulations`、`GET /simulations/{id}` | 创建/查询一次事实仿真草稿 | 可用 |
| `POST /simulations/{id}/start`、`POST /simulations/{id}/cancel`、`GET /simulations/{id}/events?after=0` | 启动、取消、按游标查进度事件 | 纪念塔规范数据可用 |
| `POST /experiments`、`GET /experiments`、`GET /experiments/{id}` | 创建/查询一批 F-00 + CF 方案 | 可用 |
| `POST /experiments/{id}/start`、`POST /experiments/{id}/cancel` | 启动/取消整批 | 纪念塔规范数据可用 |
| `GET /experiments/{id}/events?after=0`、`GET /experiments/{id}/runs` | 任务事件和完整运行级记录 | 可用；未运行时为空 |
| `GET /experiments/{id}/runtime`、`GET /simulations/{id}/runtime` | 当前真实运行、SUMO 时钟及最新 3D 坐标帧 | 可用；内存中每 0.5 秒最多更新一次，建议每秒轮询 |
| `GET /runs/{id}/trajectory?after=-1&limit=10` | 按归档块游标读取完整轨迹，响应附 WGS84 经纬度 | 可用；不修改原始归档，`limit` 为 1—20 |
| `GET /results/{experimentId}/{section}` | `section` 为 `metrics`、`comparison`、`causal-graph`、`observation`、`intervention`、`mechanism` | 真实结果封存后可读；此前 409 |

`GET /model-connections` 的 `status` 为 `ready`、`model_unavailable`、`http_error`（另返回 `httpStatus`）或 `unreachable`；该端点不代替对话服务。模型地址读取 `CROWDSIM_LLM_BASE_URL`，默认 `http://127.0.0.1:8800/v1`。期望模型 ID 优先读取 `CROWDSIM_POST_LLM_MODEL`，再读取 `CROWDSIM_LLM_MODEL`，默认 `GLM-5.3-Flash`，页面显示名为 `GLM-5.3 Flash`。可选 API Key 优先读取 `CROWDSIM_POST_LLM_API_KEY`，再读取 `CROWDSIM_MODEL_API_KEY`；与事后 Agent 使用相同配置。

## 数据集请求

`POST /datasets` 请求字段必填；前端上传数据时还需提供 `rawFile`，原件存入 GridFS：

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
| `observations[].sourceTime`、`observations[].state` | 可选字符串 | 保留原始时间标签和状态描述 |

`rawFile` 结构为 `{ "name": "...", "contentType": "text/csv", "data": "data:...;base64,..." }`；原件最大 5 MiB。服务端独立校验原件编码和解析后的标准化记录，并返回 `originalFile` 元数据。解析行的 `crowd/density/speed/pressure` 分别映射为 `population/density/meanSpeed/pressureProxy`，区域标为 `global`；时间转换为从 20:00 起的仿真秒数，原始时间保存在 `sourceTime`。

导入后返回 `id`、SHA-256 `version`、`observationCount`。后端重新验证标准化数据。`sourceKind=synthetic_reference` 的数据只作参考曲线/校准目标，**不能代替 20:00 的 SUMO 初态，也不能作为 PC 的独立运行样本**。

`POST /review-plans` 接受方案名称、可选 `datasetId`/`datasetVersion`、校准评分与确认状态、校准目标，以及完整前端方案草案。提交时校验数据集所有者/工作区和版本；草案保存到 `review_plans` 集合，不会被误当成已可运行的 SUMO 批次。

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

`runtime` 返回 `{taskId,status,progress,activeRun,frame,sequence,updatedAt,liveAvailable,availableReason,frameError}`。`activeRun` 包含 `runId/planId/experimentType/seed/runIndex/totalRuns/stepSeconds/durationSeconds/progress/phase/personCount`；`runIndex` 从 1 开始，单次 `progress` 为 0—1。`phase` 区分 `preparing/running/archiving/completed/cancelled/failed`；时钟到达总时长但仍在归档时，不增加完成次数。顶层 `progress` 沿用数据库中的完成计数，不能用单次时钟替代。

`frame={timeSeconds,persons,coordinateSystem:"WGS84",source:"sumo-traci",totalPersons,truncated}`，每个人有 `id/x/y/lng/lat/speed/angle/edgeId`。`x/y` 是 SUMO 米制坐标，`lng/lat` 用同一路网投影转换；不把米制位置作为经纬度，也不使用演示轨迹。实时显示最多返回 5000 人，超过时明确 `truncated=true`，完整 FCD 和数据库轨迹不受截断影响。运行开始/归档/结束保存 `activeRun` 元数据，逐帧只更新有界内存缓存；终态最多保留最近 64 个任务的最后帧。`availableReason=live/last_frame/awaiting_first_frame/worker_not_active` 区分实时、终态最后帧、等待第一帧及当前进程无运行帧。服务重启后 `frame=null`、`liveAvailable=false`，旧轨迹通过归档接口回放；不会把旧帧标成正在运行。

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
| 数据集元信息 | `id:string`、`version:string`、`name:string`、`sourceKind:string`、`observationCount:integer`、`createdAt:string` | 时间线分页读取；版本覆盖规范观测及存在的执行metadata |
| 仿真任务 | `id:string`、`status:string`、`datasetVersion:string`、`scenario:object`、`seed:integer`、`progress:object`、`resultId:string|null` | `progress.planned=1`；完成数和失败数均为整数 |
| 反事实批次 | `id:string`、`status:string`、`datasetVersion:string`、`seedStart:integer`、`groups:array`、`progress:object`、`resultId:string|null` | `progress` 含 `baselinePlanned/baselineCompleted/interventionPlanned/interventionCompleted/failed` 及每组 `groupId/planned/completed/failed`；计数不超过计划 |
| 运行记录 | `id:string`、`experimentId:string`、`planId:string`、`seed:integer`、`status:string`；成功后有 `metrics:object`、`featureValues:object`、`measurement`、`archive` | `planId=F-00`或已锁定组；指标为裸数值/null；同一组与种子应唯一，但数据库尚缺该复合唯一索引 |
| 事件页 | `events:array`、`nextAfter:integer` | 每条含 `sequence:integer`、`at:UTC ISO-8601 string`、`type:string`、`detail:object`；按 sequence 递增 |
| 指标分布 | `data.distributions[]`内的`groupId/metricId/n/mean/std/unit` | n是该组该指标的有效样本数；指标版本位于结果文档/运行measurement |
| 配对比较 | `data.effects[]`内的`referenceGroupId/groupId/metricId/unit/pairCount/seeds/estimate/ci95/direction/method/inferenceStatus` | 候选减实际参照；参照可为F-00或观察CF组；至少2对才有区间，不足20对为描述性估计 |
| PC 候选图 | `data.status/nodes/edges/sampleCount`及状态对应的`reason/algorithm/tests/assumptions` | 独立F-00样本；边标记collider或undirected，尚无干预支持方向字段 |

进度事件只能由执行器依据真实回执更新，每组completed+failed≤planned；completed必须关联同版本结果。API状态机预留partial，但当前SUMO执行器遇错即停止，不封存部分分析；重启仅标记interrupted，尚无补跑/重新分析恢复。当前需求与SUMO使用共同种子，没有LLM个体决策采样。完整输入、软件版本及异常恢复的扩展见对接清单。

## 指标、风险等级与分析结果

真实路径为workers/measurement.py、workers/analysis.py和workers/causal.py。当前指标post-metrics/1.1、分析post-analysis/1.0、控制post-controls/1.0。metricCatalog中的rule-model/1.0风险目录和policyTriggers是通用定义，不能据此声称执行器已运行自动广播、分流或替代路线触发。

五米裁剪网格、五秒FCD采样，输出全程全局的峰值密度、人员—时间加权均速、压力代理峰值、高风险去重人数/人时、拥堵网格累计时长和全部到达时间。高风险为密度≥3.5，拥堵还要求速度≤0.5且连续至少10秒；当前不返回四级各自的暴露数组或resource_cost。压力代理为密度乘速度向量方差，不是接触压力；主动停留计入速度和拥堵。全部到达时间取最后行走完成的最大秒数，从20:00计，全体未完成即null并标记右删失，不等于干预后安全区疏散耗时。

PC特征支持2—8项已选密度/速度/压力指标，global/A/B/C与左闭右开窗口；特征帧均值和主指标人员—时间均值口径不同。PC-Stable仅用完整独立F-00种子，至少20行且列有方差，返回样本不足、退化或部分可估计状态；只做骨架及collider标记，不做干预方向核验。

观察、干预、机制section都返回experiments和status，干预另含causalGraph；机制当前已提供相对完整观察组的消融效应，但没有完整的接收/遵从/实际改道作用链。computed只表示计算，不是机制验证结论。comparison按各组实际参照和同种子有效交集计算，至少2对提供2000次配对Bootstrap区间，不足20对标记descriptive_only。

services/metric_calculator.py、result_service.py、causal_analysis.py保留另一套协议，未接入当前执行器。不要把其中的指标envelope、features、validPairs/plannedPairs/missingReason或interventionSupportedDirections称作当前HTTP输出。精确字段、缺失解释及可取回证据见共同契约。

## 错误码

| HTTP | `error.code` | 条件 |
| --- | --- | --- |
| 400 | `INVALID_JSON`、`INVALID_QUERY` | JSON/查询参数错误 |
| 403 | `RESOURCE_FORBIDDEN` | 用户与工作区不匹配 |
| 405 | `METHOD_NOT_ALLOWED` | HTTP 方法不受支持 |
| 404 | `NOT_FOUND` | 资源或路径不存在 |
| 409 | `IDEMPOTENCY_CONFLICT`、`REQUEST_IN_PROGRESS`、`INVALID_TASK_STATE`、`DATASET_VERSION_MISMATCH`、`RESULT_VERSION_MISMATCH` | 幂等键冲突、状态/版本冲突 |
| 409 | `RESULT_NOT_READY`、`ANALYSIS_UNAVAILABLE`、`RUN_SET_MISMATCH`、`TASK_NOT_TERMINAL`、`DUPLICATE_RUN` | 结果未形成或运行集合不一致 |
| 413 | `PAYLOAD_TOO_LARGE` | 请求体超过 16 MiB |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | 非 JSON 请求体 |
| 422 | `VALIDATION_ERROR`、`SECRET_FIELD_FORBIDDEN`、`TIME_ORDER_INVALID`、`SEED_RANGE_EXCEEDED` | 字段、时间或种子无效 |
| 422 | `SCENARIO_UNSUPPORTED`、`MODEL_UNSUPPORTED`、`MODEL_VERSION_MISMATCH`、`GEOMETRY_VERSION_MISMATCH`、`RULE_VERSION_MISMATCH` | SUMO 场景、模型或版本不匹配 |
| 422 | `ACTION_UNSUPPORTED`、`ACTION_SCHEMA_MISSING`、`ACTION_PARAMETER_INVALID`、`REGION_UNMAPPED`、`METRIC_UNSUPPORTED`、`METRIC_CAPABILITY_MISMATCH`、`RESOURCE_LIMIT_EXCEEDED` | 动作、区域、指标或资源能力不满足 |
| 503 | `CAPABILITY_UNAVAILABLE` | 批量执行器尚未接入 |
| 500 | `INTERNAL_ERROR` | 服务内部错误；堆栈仅写后端日志 |

## 前端校验与待接工作

事后PostBackendWorkflow已通过同一8765 HTTP接口载入执行矩阵、保存草稿并显式启动，读取真实进度、轨迹和结果；不通过旧式WebSocket start消息执行批次。前端与后端均核对能力、版本和输入；用户仍需确认工程参数。服务未提供能力时如实停止，不能回退到演示结果。

尚待补齐模型校准与测量有效性、实际控制/机制证据、窗口区域终点、到达口径、软件/文件复现材料，以及后端部分结果和一致性恢复。负责层次、产物及验收标准见[SUMO与后端对接清单](../docs/post_sumo_handoff.md)。
