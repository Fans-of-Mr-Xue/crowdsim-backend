# modeling_algorithm

人工社会建模算法按三大模型分层：

- `individual_model/`：感知、记忆、情感、欲望/目标、博弈、行为决策、动作、学习。
- `environment_model/`：环境因素与环境响应理论/方法。
- `rule_model/`：人口分配、SUMO 运动、观测、调度、可视状态、消息控制。

本轮可执行链路：

`PAD 连续情感模型 → 情感驱动欲望 → 有限理性博弈 → LLM-driven → 经验回放`

`registry.py` 是前端中文方法名与后端模块路径的唯一注册表。`IMPLEMENTED = False` 的文件仅保留稳定路径，调用时会显式抛出 `AlgorithmNotImplementedError`，不会以占位逻辑冒充算法实现。
