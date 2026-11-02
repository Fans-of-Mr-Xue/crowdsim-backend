# 事后需求界定与方案快照对接（2026-10-22）

2026-10-30补充：当前可执行方法、指标与结果字段以[执行与分析共同契约](post_execution_analysis_contract.md)为准；SUMO和后端待实现项见[对接清单](post_sumo_handoff.md)。本文保留现有HTTP字段，不将设计充分性建议写成已实现的结构化校验。

当前流程为：上传数据（含事实基线核对确认）、明确用户目的、查看与修改人工社会、统一反事实方案设计。左侧“方案预览”打开只读快照。右侧只显示当前流程内容，取消总览及工作台内部导航；旧 `postchat/parameters` 总览跳转路由已删除。后续仍为反事实运行、观察分析、干预分析、机制分析、复盘报告；三段分析页面保持原布局。

新方案使用 `reviewIntake.schemaVersion=2`，包含 `baselineConfirmed/goalConfirmed/societyConfirmed/designConfirmed/planConfirmedAt`。统一实验池保存在 `counterfactualDesign={schemaVersion:2,groups,unit,scope,controlledVariables,repeatCount,executionSettings}`；每组为 `id/name/hypothesis/method/factorSymbols/primaryMetricId/expectedDirection/decisionRule/execution`。方法为paired/ofat/ablation，参照可为F-00或本池其他组，不按三段分析分组。变量定义继续使用variableDefinitions。实际改变变量、完整参照、循环引用、主要指标、重复数与数值均受后端校验。

人工社会原模型可在右侧查看，修改记录保存在 `artificialSociety={schemaVersion:1,baseModel,overrides:{agents,environment,rules}}`，作为各组共同条件进入持久化快照。当前执行器未接入任意文本模型改动；非空overrides可保存，但执行模板返回SOCIETY_MAPPING_REQUIRED，不能静默忽略。修改目标、变量、组、共同条件或社会配置后完整确认失效。

新对话上下文设置 `designSchemaVersion=2`、`designStage=counterfactual`、`counterfactualDesign`、`variableDefinitions`、基线与目的确认状态、执行能力；答复仅包含assistant_message和可选完整groups、repeat_count。新执行组为experimentType=counterfactual。观察筛选全部组；干预筛选实际单变量或零差异对照；机制筛选有完整策略参照且实际隔离一个模块的消融组，同一组可复用。PC仅用独立F-00样本。分析接口保持原字段，页面无需改变。

以下示例及分类型协议保留为**历史v1兼容协议**，新工作台不再使用逐类确认或三类分组。

## 可直接用于接口的示例

| 文件 | 接口 | 说明 |
|---|---|---|
| [post_review_dataset.json](examples/post_review_dataset.json) | `POST http://127.0.0.1:8765/api/v1/post/datasets` | 标准化时序；缺失指标为 `null`，没有伪造的压力值 |
| [post_review_chat.json](examples/post_review_chat.json) | `POST http://127.0.0.1:8767/post-review/chat` | 供 PostAgent 处理的当前类型设计请求 |
| [post_review_plan.json](examples/post_review_plan.json) | `POST http://127.0.0.1:8765/api/v1/post/review-plans` | 已确认三类实验的完整草案；可保存，不代表可执行 SUMO 批次 |

数据与方案接口请求头：

```http
Content-Type: application/json
X-CrowdSim-User: crowdsim-local-user
X-CrowdSim-Workspace: post-post-example-001
Idempotency-Key: example-dataset-001
```

`Idempotency-Key` 同一次请求重试时复用；数据与方案应分别使用不同的键。前端工作区使用 `post-` 加上经过字符清理并截取至 59 字符的对话 ID。Agent 对话接口仅需要 JSON 内容类型，不接受前端提交的 system 指令。

示例方案的 `datasetId`、`datasetVersion` 为 `null`，用于保存未绑定数据集的设计草案。正式接入时，先提交数据示例，从返回的 `data.id` 与 SHA-256 `data.version` 取值，同时替换方案顶层、`calibrationTargets`、`plan.factualBaseline` 中的数据引用。不能自行编造数据集 ID 或版本；后端按用户、工作区与实际版本检查。

## 三种基线来源

1. 独立上传：界面支持 UTF-8 CSV、JSON、Markdown，必需字段为 `time/crowd/density/speed/pressure`；选择实测或合成性质。JSON 可包含 `name/location/sourceKind/rows`。前端解析校验后转为数据接口的 `observations`，原件以 Base64 `rawFile` 入库。`timeSeconds` 是相对时间；数值范围和重复时空记录由后端复核。
2. 事中实验：当前工作台分页读取后端历史对话元数据，从事中对话各自的 `during-<conversationId>` 工作区读取分页实验目录；本地索引只补充来源，不再作为唯一目录。列表只显示含已完成轮次且携带原始观测绑定的实验；读取后仅允许选择 `valid===true && status=completed` 且具有原始时序的一轮。导入前重新读取报告与实验，核对六项需求/模型绑定、executionVersion、所选轮次restartCount、seed和实际series。版本变更、读取失败或对话切换时不向当前需求写入旧结果。

   通过现有事后数据接口复制到 `post-<当前对话>`，不修改事中实验；metadata保存完整sourceBinding（含reportVersion、observationEvidenceVersion等）、原始sourceSeries、观测间隔、指标与缺失项。事中末帧允许小数秒：仅 `metadata.sourceBinding.sourcePhase=during` 的导入保留精确数值时间，普通上传仍遵守原整数秒约束；人数/速度为零原样保留，未测压力为null。聊天和方案只保存来源摘要与数据集引用，完整原始时序仍在事后数据集。保存后仍需用户确认事实基线，再沿原四步流程继续。没有原始时序或有效完成轮次的实验不会被当成可导入数据。
3. 网络资料：`historical_event` 先在后端收集公开网页/PDF 正文，返回来源标题、URL、读取时间和内容校验和。自动搜索需配置搜索服务；用户直接提供的链接可独立读取。模型只整理实际读取的正文。网络资料可形成待确认的事件事实摘要；数值时间线仍须单独接入并校验。没有来源时不允许确认事实基线。

数据检查通过后仍须用户明确确认基线。`baseline_review` 可以根据用户明确补充修正名称、地点、时窗显示标签、说明；不能改写原始数值、数据版本、来源性质或代用户确认。

## Agent / skill 和工作台

| task | 后端加载的 skill | 输出与用途 |
|---|---|---|
| `intake`、`baseline_review`、`goal_review`、`historical_event` | `post_data_assessment` | 数据接入、基线核对、目标规范化、来源梳理 |
| `design` | `post_causal_hypothesis`、`counterfactual_design` | v2统一实验池建议；旧v1仍兼容分类型结构 |
| `parameter_defaults`、`parameter_intent` | `counterfactual_design` | 兼容原有参数问答 |
| `pc_defaults` | `post_causal_hypothesis`、`counterfactual_design` | 已绑定执行能力的干预设计中建议PC特征，不运行PC |

后端加载 `agent/PostAgent.agent.md` 与白名单 skill 全文，并校验 JSON。`goal_review` 返回 `assistant_message`、可选 `proposed_goal`、`missing_fields`；目标建议必须由用户确认后采用。

goal_review、design、pc_defaults及两种兼容参数问答会同时注入共同契约正文。当前组答复没有primaryMetric/decisionRule等新字段；主要终点、控制项、范围、判断条件和缺口使用既有hypothesis/change及assistant_message表达。manipulatedFeatureId由工作台设置；不能在对话组答复中新增该字段。工作台executionSettings、组execution和HTTP执行矩阵是不同层级。

设计请求包含 `designStage=observation|intervention|mechanism`、`availableFactors`、`schemeCatalog`、`experimentSchemes`。`experiments` 只替换当前类型；`experiment_updates` 只更新当前类型已有 ID，不影响其他类型。每组明确 `id/name/factor/change/hypothesis/method`。解释性问题可以只返回 `assistant_message`，不会自动改变工作台。总组数为 3–12，每组重复为 1–50。

三类实验分别确认；改变本类实验使本类确认失效，改变目标、基线、公共变量或重复次数使相关确认失效。`reviewIntake` 记录版本、基线/目的确认、三类确认和完整方案确认时间。方案保存接口再次检查完整性，并将草案与引用保存到 MongoDB。未实测校准评分为 `null`，不补成默认分数。

对话及 intake/方案元数据通过已有 `/conversations/sync` 同步到后端；每个对话独立保存。完整时序保存在数据服务，聊天记录和方案提交只保存基线摘要及数据集引用，避免重复大数组。前端丢失本地缓存后可从对应历史对话恢复目的、步骤和方案。

## 联网配置与运行边界

在后端 `.env` 设置 `CROWDSIM_POST_SEARCH_API_KEY`，可选 `CROWDSIM_POST_SEARCH_URL`（默认 Tavily 的 HTTPS 搜索入口）。实现使用 Bearer 请求头及 `query/max_results/include_raw_content` 等字段；协议参见 [Tavily 官方接口文档](https://docs.tavily.com/documentation/api-reference/endpoint/search)。密钥只在后端使用。未配置时返回 `needs_sources` 并引导提供公开链接；读取失败保留错误说明，不生成来源或观测值。

对话服务需要 `Crowdbackend/requirements.txt` 中的 PyYAML；公开资料读取还使用现有 aiohttp、pypdf 和 BeautifulSoup 组件。数据和方案入库需要现有 8765 服务及 MongoDB；8767 负责对话存储与 PostAgent。更改后需要重启后端服务。

确认完整方案只保存并跳转到快照。2026-10-23起纪念塔测试映射已接入独立F-00/CF执行器；仍需在实验运行页明确确认工程矩阵，再显式启动。其他场景缺少能力时如实返回不可执行，不能将草案保存或基线确认称为实验完成。完整数据、执行参数及Mongo归档规范见[纪念塔事后完整测试](post_memorial_end_to_end_test.md)。
