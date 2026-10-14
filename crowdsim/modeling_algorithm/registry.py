"""Stable registry between front-end method labels and backend modules."""

from __future__ import annotations

from copy import deepcopy


_ALGORITHMS = [
    # Individual model: perception and memory.
    ("individual", "perception", "局部邻域观测", "individual_model.perception.local_neighborhood_observation", False),
    ("individual", "perception", "事件订阅", "individual_model.perception.event_subscription", False),
    ("individual", "perception", "视野与距离过滤", "individual_model.perception.vision_distance_filter", False),
    ("individual", "memory", "有限状态记忆", "individual_model.memory.finite_state_memory", False),
    ("individual", "memory", "短期情境记忆", "individual_model.memory.short_term_context_memory", False),
    ("individual", "memory", "规则知识库", "individual_model.memory.rule_knowledge_base", False),
    # Individual model: implemented path and reserved alternatives.
    ("individual", "emotion", "离散情绪状态机", "individual_model.emotion.discrete_emotion_state_machine", False),
    ("individual", "emotion", "PAD 连续情感模型", "individual_model.emotion.pad_continuous", True),
    ("individual", "emotion", "OCC 事件评价模型", "individual_model.emotion.occ_event_appraisal", False),
    ("individual", "desire", "情感驱动欲望", "individual_model.desire_goal.emotion_driven_desire", True),
    ("individual", "desire", "需求优先级队列", "individual_model.desire_goal.need_priority_queue", False),
    ("individual", "desire", "风险规避效用", "individual_model.desire_goal.risk_avoidance_utility", False),
    ("individual", "goal", "BDI 目标栈", "individual_model.desire_goal.bdi_goal_stack", False),
    ("individual", "goal", "有限状态目标", "individual_model.desire_goal.finite_state_goal", False),
    ("individual", "goal", "分层任务目标", "individual_model.desire_goal.hierarchical_task_goal", False),
    ("individual", "game", "演化博弈", "individual_model.game.evolutionary_game", False),
    ("individual", "game", "有限理性博弈", "individual_model.game.bounded_rationality", True),
    ("individual", "game", "多主体强化学习", "individual_model.game.multi_agent_reinforcement_learning", False),
    ("individual", "decision", "规则决策树", "individual_model.decision.rule_decision_tree", False),
    ("individual", "decision", "效用最大化", "individual_model.decision.utility_maximization", False),
    ("individual", "decision", "有限状态机", "individual_model.decision.finite_state_machine", False),
    ("individual", "decision", "BDI 推理", "individual_model.decision.bdi_reasoning", False),
    ("individual", "decision", "LLM-driven", "individual_model.decision.llm_driven", True),
    ("individual", "action", "SUMO striping", "individual_model.action.sumo_striping", False),
    ("individual", "action", "社会力运动", "individual_model.action.social_force_motion", False),
    ("individual", "action", "混合运动控制", "individual_model.action.hybrid_motion_control", False),
    ("individual", "learning", "参数自适应", "individual_model.learning.parameter_adaptation", False),
    ("individual", "learning", "经验回放", "individual_model.learning.experience_replay", True),
    ("individual", "learning", "无在线学习", "individual_model.learning.no_online_learning", False),
    # Environment layers and factors.
    ("environment", "layer", "空间拓扑", "environment_model.layers.spatial_topology", False),
    ("environment", "layer", "动态环境", "environment_model.layers.dynamic_environment", False),
    ("environment", "layer", "信息环境", "environment_model.layers.information_environment", False),
    ("environment", "layer", "执行接口", "environment_model.layers.intervention_interface", False),
    ("environment", "factor", "局部人群密度", "environment_model.factors.local_crowd_density", False),
    ("environment", "factor", "出入口流率", "environment_model.factors.entrance_exit_flow", False),
    ("environment", "factor", "环境可见度", "environment_model.factors.visibility", False),
    ("environment", "factor", "通道通行能力", "environment_model.factors.passability", False),
    ("environment", "factor", "区域风险水平", "environment_model.factors.regional_risk", False),
    ("environment", "factor", "信息覆盖强度", "environment_model.factors.information_coverage", False),
    # Environment response theories and methods.
    ("environment", "response", "基本图模型", "environment_model.responses.fundamental_diagram", False),
    ("environment", "response", "社会力模型", "environment_model.responses.social_force", False),
    ("environment", "response", "元胞传输模型", "environment_model.responses.cell_transmission", False),
    ("environment", "response", "排队论", "environment_model.responses.queueing_theory", False),
    ("environment", "response", "瓶颈流理论", "environment_model.responses.bottleneck_flow", False),
    ("environment", "response", "动态交通分配", "environment_model.responses.dynamic_traffic_assignment", False),
    ("environment", "response", "势场理论", "environment_model.responses.potential_field", False),
    ("environment", "response", "风险感知理论", "environment_model.responses.risk_perception", False),
    ("environment", "response", "认知地图", "environment_model.responses.cognitive_map", False),
    ("environment", "response", "信息扩散模型", "environment_model.responses.information_diffusion", False),
    ("environment", "response", "社会影响理论", "environment_model.responses.social_influence", False),
    ("environment", "response", "阈值传播模型", "environment_model.responses.threshold_diffusion", False),
    ("environment", "response_method", "分段函数", "environment_model.responses.piecewise_function", False),
    ("environment", "response_method", "连续动力学", "environment_model.responses.continuous_dynamics", False),
    ("environment", "response_method", "网格离散更新", "environment_model.responses.grid_discrete_update", False),
    ("environment", "response_method", "服务率队列", "environment_model.responses.service_rate_queue", False),
    ("environment", "response_method", "容量约束", "environment_model.responses.capacity_constraint", False),
    ("environment", "response_method", "最短时间重路由", "environment_model.responses.shortest_time_rerouting", False),
    ("environment", "response_method", "栅格势场", "environment_model.responses.grid_potential_field", False),
    ("environment", "response_method", "边权动态调整", "environment_model.responses.dynamic_edge_weight", False),
    ("environment", "response_method", "规则触发", "environment_model.responses.rule_trigger", False),
    ("environment", "response_method", "覆盖半径", "environment_model.responses.coverage_radius", False),
    ("environment", "response_method", "网络扩散", "environment_model.responses.network_diffusion", False),
    ("environment", "response_method", "个体阈值更新", "environment_model.responses.individual_threshold_update", False),
    # Rule model.
    ("rule", "runtime", "人群分配", "rule_model.population_allocation", False),
    ("rule", "runtime", "SUMO 运动", "rule_model.sumo_motion", False),
    ("rule", "runtime", "局部观测", "rule_model.local_observation", False),
    ("rule", "runtime", "决策调度", "rule_model.decision_scheduling", False),
    ("rule", "runtime", "可视状态", "rule_model.visual_state", False),
    ("rule", "runtime", "消息与控制", "rule_model.message_control", False),
]


def algorithm_catalog() -> list[dict[str, object]]:
    return deepcopy([
        {"model": model, "module": module, "method": method, "module_path": module_path, "implemented": implemented}
        for model, module, method, module_path, implemented in _ALGORITHMS
    ])


def method_status(method: str) -> dict[str, object] | None:
    return next((item for item in algorithm_catalog() if item["method"] == method), None)
