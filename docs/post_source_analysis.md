# 事后源算法适配与调用

源仓库固定为用户指定的后端 `oldblack45/MACE@25489c09` 与前端 `Fans-of-Mr-Xue/MACE-FE2@db18e913`。只适配 crowdSim 事后阶段。具体源文件及 SHA256 见 `algorithms/powergame/SOURCE.json`；其 `.gitattributes` 保持校验所需的原始换行字节。

## 先核对既有实现

| 源功能或事后方法 | 已有实现及本次处理 | 实际调用 |
| --- | --- | --- |
| OAT 末态指标绝对差、相对差 | 复用 `algorithms.metrics.compare_metrics`，方向设为 up，保持 candidate−reference 及除以基线绝对值的源定义 | `source_analysis._metric_comparison` |
| 共同种子配对比较 | 复用 `paired_mc.paired_monte_carlo`，不再在 worker 重写自助抽样 | `analysis.paired_effect` |
| OFAT | 复用 `ofat.ofat_scan`，回调读取已归档结果，不再运行一次仿真 | `analysis.paired_effect(design_method='ofat')` |
| 机制消融 | 复用 `mechanism_ablation.mechanism_ablation`，保留实际参照组 | `analysis.paired_effect(design_method='ablation')` |
| PC-Stable | 保留现有 `workers.causal.discover → algorithms.pc` | `analysis.summarize` |
| 数值关联、图关键节点、滞后边、已声明回路 | 复用 `numeric`、`mechanism_graph`，不新增同义实现 | `source_analysis._micro_macro` |
| DAG 路径、杠杆、线性传播 | 已有 `causal_graph`，无需重新搬运 | 保持现有显式图输入契约；不将模型提及率送入 do 传播 |
| 轨迹统计、代表世界、转折点、时序指标 | 原本缺失：抽出源 `TrajectoryAnalysisService` 的无依赖完整类，逻辑不变 | `complete_analysis` |
| 每世界逐时点结局预测、全曲线关键点解释 | 原本缺失：原样引入 `outcome_forecast.py` | `complete_analysis` |
| 条件等权均值差、证据约束路径抽取、世界共同提及路径 | 原本缺失：原样引入 `horizontal_analysis` 的 contract/dynamic_catalog/extraction/analysis | `complete_analysis`，始终显式传 crowd catalog 与 GuardedLLMExtractor |
| 新版逐回合加权门控数值核 | 已有机制图库缺少 rounds 分支：在同一模块扩展 `compute_round_plays` 与原有汇总入口 | 只接受显式模型动因/权重/同量纲阈值；不会从相关系数自动生成这些输入 |

源算法的政治主体词表、规则匹配示例不适用于人群，替换为根据实际字段及带证据的实际动作生成的词表。没有调用源 RulesExtractor 作模型失败的兜底。源前端的阵营叙事、冲突规律库与占位权重不是人群观测，也不作为人群机制输入。

共享统计核的区间使用既有 `paired_summary` 的百分位插值；旧 worker 使用自助样本固定索引。点估计与共同种子定义不变，新增 `algorithm`、`ciMethod`、`standardError` 说明实际计算入口。仅有一个配对时仍只返回描述性差值。

## 真实调用链

`SumoBatchExecutor._run → _one（事中原生决策/信息/执行链）→ summarize → source_analysis.complete_analysis → GridFS 工件 → result 封存 → 既有三类结果接口 / analysis-data → postAnalysisResults.js`。

统一反事实组按已冻结方案里的 method 分派；实际单因素与消融资格仍由原有动作隔离校验决定。非合格组不因方法名称获得已验证的 OFAT/消融结论。每次完整 run/seed 才是独立样本。

新源分析只对 `post-during-runtime/1.0` 运行自动执行，处于既有执行线程内，没有新增常驻服务。取消会同时中止正在等待的异步 HTTP 请求；暂停沿用原检查点。正常关闭仍走既有全部服务、线程和子进程清理。

## 输入适配

每个世界对应一个真实运行，最多划分八个等时区间。**所有个体记录**参与区间计数；宏观状态取区间内最后一条真实观测，不插值、不补造人员。区间不是独立蒙特卡洛样本。

每个预测点的时钟为该区间实际输入证据的最晚时点，不会把晚于宏观采样的个体记录放到更早的预测时间。没有宏观采样的区间仍保留已验证行为计数，但不填造密度或速度；元数据分开标明 macroObservationSeconds 与 observedThroughSeconds。

状态包含密度（人/m²）、速度、压力代理、区域人数，以及移动/停止/加速/减速的人时采样数、消息送达与信任次数、实际应用决策次数。最终响应变量沿用已实现的人群指标及单位，包括峰值密度、速度、压力代理、风险暴露等；缺失指标不补零。

`decision.action` 只包含实际执行回执为 applied 的个体动作。被拒绝的提案单独记录，不当成已采取动作。批次暂停、继续、资源释放等用户操作不进入分析动作。决策证据摘要保留真实 profile 中的风险/拥挤容忍、耐心等参数、实际观测、已知事件、候选入口的收益门槛和换路冷却时间。每区间最多带两条执行上下文示例，全部原始个体证据仍在归档文件中。

原生控制方案整体作为源 `strategy` 因素的水平，避免凭空声称有完整多因素交叉实验。每个候选与其实际 referenceGroupId 另算条件差异；主推断仍以同种子的配对区间为准。

预测类别沿用现有图表的稳定通行、局部拥堵、高密拥堵、疏散恢复。源逐时点预测只读取当前及过去的输入前缀，终点是整个复盘运行窗口末尾。全曲线关键点解释是独立阶段。`groupTimelines` 只是有效世界概率分布的均值及真实观测时点，附有效/总世界数，不以经验状态占比冒充模型预测。

## 机制证据的含义

源路径输出保留引用校验、尝试记录、独立世界共同提及及真实指标单位。score 是提及比例或条件均值差，不能充作因果置信度或动力学权重。

微观到宏观图复用既有 Pearson 和图汇总算法，只描述同一运行内区间行为计数与宏观指标的关联，字段明确标记 `causalEffect=false`。无变化或缺少证据时不输出边。不推定引导反应滞后，不预设回路。

后端已有的个体容忍阈值、入口换路门槛和显示密度阈值各有独立含义。它们不能直接替换原加权净推力门槛。原回合门控不自动运行，返回 `roundGateStatus=requires_explicit_model_driver_weights_and_thresholds`；没有把相关系数、消息比例或 LLM 文本补成权重。现有密度阈值不改为必然事故判定。

## 存储及读取

不需要给原上传的区域观测表新增必填列或迁移 SQL 表。结果新增 `analysisPipeline` 与各 section 的 `sourceAnalysis`；大工件保存在现有 GridFS：`source_analysis.json.gz`，包含完整预测、路径、引用、尝试记录、每世界微宏观图及 world_ref 到 runId/seed/groupId 的映射。结果中保存版本、输入 SHA256、文件 SHA256、大小及所有者/工作区/实验/数据版本绑定。

`GET /api/v1/post/results/{experimentId}/analysis-artifact?offset=0&limit=65536` 返回 base64 字节块，最大 256KiB。按 nextOffset 拼接、校验 SHA256 后解 gzip 和 UTF-8；客户端不能任意传 fileId。与既有 `/results/{experimentId}/{observation|intervention|mechanism}` 使用同一实验标识。

Mongo 结果中只保留最多十六个世界的原始预测和最多一百二十八条路径边摘要；所有世界的概率聚合曲线及完整 GridFS 工件保留。前端仅改既有数据适配器：概率曲线与结论区绑定新输出。原 PC 图不把候选提及率画成置信度，原结果页布局、菜单、图表组件和交互不改。

模型配置沿用当前后端 GLM/环境变量。模型不可达、超时或响应不满足源契约时，相应结果明确标记 partial/unavailable，已有数值结果仍可封存读取，不生成假概率和假路径。连接或超时失败后本批次后续模型调用快速返回不可用。历史结果不会被自动重算覆盖；新运行或原批次恢复后的封存使用新链路。

## 验证

`tests/test_post_source_analysis.py` 检查源文件 hash、实际共享核分派、概率与候选引用、输入 SHA256/权限绑定、损坏证据、缺失响应、拒绝动作、取消 HTTP 等。带 SUMO_BINARY 的测试使用真实 SUMO 自动执行 F-00 与原生引导方案，并读取三个结果 section 及压缩工件；仅模型回复使用明确的录制测试实现，不据此声明模型质量。前端 `postAnalysisResults.test.mjs` 验证新概率绑定、真实时点、零值、候选关系标记及离线时数值结果保留。不执行 npm 构建。
