# 事后 Agent 工具契约

`tool_loader.create_agent_registry(agent_definition)` 仅装载 Agent frontmatter 中的工具名，并在 Agent 声明 skill 时提供已有的 `skill` 元工具。名称保持原文，包括 `serch_case_library` 和 `intrudec_platform`。

调用方使用 `PostToolContext(app, user, workspace)` 注入已初始化的 `PostAnalysisApp` 与当前身份；可再注入 `confirmed_goal`、按 Agent/阶段筛选后的 `skill_registry`、`mode`、`phase` 及同名 `services`。用户和工作区沿用事后 API 的资源隔离契约。工具执行返回 JSON；未装配的同名服务返回 `TOOL_UNAVAILABLE`。

已对接的能力：标准化数据集校验/注册/读取、独立样本 PC、能力查询、实验方案结构校验、实验草案创建与真实启动、进度事件、已封存结果及三段结构化读取。提交前须有用户确认；执行器未就绪时只创建 `draft`，返回 `started: false`。`validate_experiment_design` 的 `valid` 只表示请求结构通过校验；`executable` 还要求执行器就绪且能力、区域和动作校验通过。真实启动仍由 `PostAnalysisApp.start` 核对数据版本与任务状态。

`run_pc_analysis` 要求调用方明确确认各行是独立样本（`independentUnitsConfirmed: true`），还会检查样本量、字段一致性、有限数值与非恒定列。单次观测时间线的相邻帧不能直接满足这一条件；不满足时返回准备不足，不生成候选图。

待接入的同名服务：事中快照、事前案例库、因果存储、真实 SUMO 配置编译、三段 LLM 问答与报告持久化。现有 `postanalysis_api` 的批量执行器仍返回 `SUMO_BATCH_ADAPTER_MISSING`，机制分析需要真实事件证据。前端尚未将这些工具接入对话循环，因此本目录提供后端可装配契约，不代表页面已经端到端运行。
