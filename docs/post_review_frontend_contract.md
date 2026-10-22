# 事后需求界定与方案快照对接（2026-10-22）

本次接口服务于既有“事后：复盘解释”流程。顺序是：接入数据、明确确认基线、规范化并确认目的、逐类设计和确认观察/干预/机制实验、确认完整方案、进入方案设计快照。后续仍为反事实运行、观察分析、干预分析、机制分析、复盘报告。

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
2. 事中实验：事后读取当前用户已关联事中实验的报告，从 `valid=true && status=completed` 的轮次中明确选择一轮。保留 `conversationId/experimentId/runId/seed/reportVersion/requirementId/modelId/snapshotId` 及可用指纹；从 `series` 读取原始人数、全局密度与全局速度，未测指标保留为空。通过数据接口复制到本次事后工作区，记录新数据集 ID 和版本；不覆盖事中记录。
3. 网络资料：`historical_event` 先在后端收集公开网页/PDF 正文，返回来源标题、URL、读取时间和内容校验和。自动搜索需配置搜索服务；用户直接提供的链接可独立读取。模型只整理实际读取的正文。网络资料可形成待确认的事件事实摘要；数值时间线仍须单独接入并校验。没有来源时不允许确认事实基线。

数据检查通过后仍须用户明确确认基线。`baseline_review` 可以根据用户明确补充修正名称、地点、时窗显示标签、说明；不能改写原始数值、数据版本、来源性质或代用户确认。

## Agent / skill 和工作台

| task | 后端加载的 skill | 输出与用途 |
|---|---|---|
| `intake`、`baseline_review`、`goal_review`、`historical_event` | `post_data_assessment` | 数据接入、基线核对、目标规范化、来源梳理 |
| `design` | `post_causal_hypothesis`、`counterfactual_design` | 当前类型实验的建议及结构化变更 |
| `parameter_defaults`、`parameter_intent` | `counterfactual_design` | 兼容原有参数问答 |

后端加载 `agent/PostAgent.agent.md` 与白名单 skill 全文，并校验 JSON。`goal_review` 返回 `assistant_message`、可选 `proposed_goal`、`missing_fields`；目标建议必须由用户确认后采用。

设计请求包含 `designStage=observation|intervention|mechanism`、`availableFactors`、`schemeCatalog`、`experimentSchemes`。`experiments` 只替换当前类型；`experiment_updates` 只更新当前类型已有 ID，不影响其他类型。每组明确 `id/name/factor/change/hypothesis/method`。解释性问题可以只返回 `assistant_message`，不会自动改变工作台。总组数为 3–12，每组重复为 1–50。

三类实验分别确认；改变本类实验使本类确认失效，改变目标、基线、公共变量或重复次数使相关确认失效。`reviewIntake` 记录版本、基线/目的确认、三类确认和完整方案确认时间。方案保存接口再次检查完整性，并将草案与引用保存到 MongoDB。未实测校准评分为 `null`，不补成默认分数。

对话及 intake/方案元数据通过已有 `/conversations/sync` 同步到后端；每个对话独立保存。完整时序保存在数据服务，聊天记录和方案提交只保存基线摘要及数据集引用，避免重复大数组。前端丢失本地缓存后可从对应历史对话恢复目的、步骤和方案。

## 联网配置与运行边界

在后端 `.env` 设置 `CROWDSIM_POST_SEARCH_API_KEY`，可选 `CROWDSIM_POST_SEARCH_URL`（默认 Tavily 的 HTTPS 搜索入口）。实现使用 Bearer 请求头及 `query/max_results/include_raw_content` 等字段；协议参见 [Tavily 官方接口文档](https://docs.tavily.com/documentation/api-reference/endpoint/search)。密钥只在后端使用。未配置时返回 `needs_sources` 并引导提供公开链接；读取失败保留错误说明，不生成来源或观测值。

对话服务需要 `Crowdbackend/requirements.txt` 中的 PyYAML；公开资料读取还使用现有 aiohttp、pypdf 和 BeautifulSoup 组件。数据和方案入库需要现有 8765 服务及 MongoDB；8767 负责对话存储与 PostAgent。更改后需要重启后端服务。

确认完整方案只保存并跳转到快照。现有 F-00/CF 批量执行器未接入的状态保持如实返回，不能将草案保存或基线确认称为真实实验已完成。
