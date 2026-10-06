# 事后阶段通用算法库

本目录提供不依赖前端页面、HTTP、SUMO 或特定案例数据的 Python 计算函数。本轮只建立后端算法库；现有事后页面仍使用原有 JavaScript 实现，尚未调用这里的函数。

新增实现以 [`Crowd_Counterfactual_Formulas_Algorithms.md`](Crowd_Counterfactual_Formulas_Algorithms.md) 的公式和伪代码为准，仍不绑定具体场景。`metrics.py` 汇总影响因素、响应指标与配对指标效应；每种实验或发现算法分别在独立文件中：

| 文档方法 | 文件 | 主要入口 |
| --- | --- | --- |
| A1 仿真随机对照 | `randomized_simulation.py` | `randomized_simulation` |
| A2 配对蒙特卡洛 | `paired_mc.py` | `paired_monte_carlo` |
| A3 时序敏感度 | `timing_sensitivity.py` | `timing_sensitivity` |
| B1 一次一因子 | `ofat.py` | `ofat_scan` |
| B2 二因子全因子 | `two_factor.py` | `two_factor_effects` |
| B3 多因子正交 | `orthogonal_design.py` | `orthogonal_design_analysis` |
| C1 机制消融 | `mechanism_ablation.py` | `mechanism_ablation` |
| C2 线性中介效应 | `mediation.py` | `linear_mediation` |
| C3 阈值级联与阻断 | `cascade_path.py` | `threshold_cascade`、`compare_blocked_channels` |
| PC/PC-stable | `pc.py` | `fisher_z_ci`、`pc_discovery` |
| 可选全局敏感度 Sobol | `sobol.py` | `sobol_indices` |
| 可选 Morris 筛选 | `morris.py` | `morris_elementary_effects` |

运行仿真的方法通过 `simulate` 回调接入任意仿真器，或读取完整运行级别的结果数组。本库不提供真实 SUMO 接口，也不从同一轨迹的时间步制造独立样本。置信区间使用完整独立运行块的百分位自助抽样；运行失败会被返回为不完整结果或直接报错，不会静默删去失败组。中介模块只实现文档中的**线性、无 A×M 交互**模型，且要求调用方显式确认时序和识别假设。正交设计需要调用方提供已选设计表并验证两两平衡及可估秩；PC 的经典假设、未定向边与冲突标记见模块说明。

指标由 `metrics.py` 计算：九项影响因素分别通过 `aggregation_start`、`population_factors` / `active_population_counts`、`arrival_rates`、`gate_capacity`、`response_delays`、`normalized_intensity`、`information_coverage`、`bottleneck_width` 等函数组合得到；四项响应指标由 `peak_grid_density`、`mean_movement_speed`、`max_contact_pressure`、`evacuation_time` 给出。`kernel_crowd_pressure` / `max_kernel_crowd_pressure` 是与机械接触压力**不同**的可选人群压力指标。调用方须传入固定网格有效面积、时间步长、目标人群、安全到达记录，以及接触模型所需的半径和刚度；缺失数据返回 `None` 或显式报错，不用本场景默认值填补。

本次未实现离散数据 G² 版 PC、非线性中介模型、正交表自动生成和敏感性假设检验；对应函数会要求连续高斯数据、线性中介模型或调用方提供设计表。输出仅表示给定仿真模型中的统计或干预估计，不能直接称为现实人群的因果效应。

| 模块 | 本轮实现 | 对应前端实现 |
| --- | --- | --- |
| `numeric.py` | 均值、总体标准差、Pearson 相关、控制变量残差化与偏相关、弱相关条件集筛选 | `postCrowdCausalGraphExtractor.js` |
| `causal_graph.py` | 风险路径枚举、路径总效应、关键中介节点、DAG 上的线性干预传播 | `causalAnalysisEngine.js` |
| `mechanism_graph.py` | 微观关键节点评分、滞后边排序、预定义回路解析 | `mechanismAnalysisEngine.js` |
| `metrics.py` | 事实与反事实指标的方向化差值、改善率、加权评分 | `postCounterfactualRuntime.js` 的指标与评分部分 |

输入使用普通 Python 字典和列表；图节点与边的字段沿用现有前端的 `id`、`source`、`target`、`effect`、`confidence` 等命名。事后页面可在后续新增接口时序列化这些数据并调用本库。

```python
from algorithm import compare_metrics, enumerate_risk_paths, simulate_intervention

definitions = [{"key": "peakDensity", "label": "峰值密度", "direction": "down", "unit": "人/m²"}]
comparison = compare_metrics({"peakDensity": 5.1}, {"peakDensity": 4.2}, definitions)

nodes = [
    {"id": "control", "name": "分流", "tier": "treatment"},
    {"id": "risk", "name": "风险", "tier": "outcome"},
]
edges = [{"source": "control", "target": "risk", "effect": -0.4, "confidence": 0.8, "verified": False}]
paths = enumerate_risk_paths(nodes, edges, "risk")
result = simulate_intervention(nodes, edges, {"nodeId": "control", "delta": 0.5}, "risk", baseline=78)
```

数值算法只处理传入的观测值或效应估计，不生成仿真结果。相关与偏相关是关联度筛选，不能单独证明因果；`simulate_intervention` 假设输入图无环、边效应线性且已由外部确定。指标基线为零时改善率返回 `None`，缺失指标不填入示例数值。
