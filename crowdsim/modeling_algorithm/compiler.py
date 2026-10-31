"""Compile confirmed modeling requirements into an executable artificial society."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any

from crowdsim.domain.artificial_society_spec import ArtificialSocietySpec
from crowdsim.domain.crowd_visual_state import CrowdVisualPolicy
from crowdsim.infrastructure.artificial_society_repository import ArtificialSocietyRepository
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.modeling_algorithm.registry import algorithm_catalog
from crowdsim.modeling_algorithm.rule_model.catalogue import (
    STRONG_RULES,
    default_rule_methods,
    implemented_rule_method,
    resolve_rule_method,
    strong_rule_catalogue,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: Six mandatory strong constraints, sourced from the rule-model execution catalogue.
REQUIRED_RULES = tuple((item["id"], item["category"], item["name"]) for item in STRONG_RULES)
REQUIRED_RULE_IDS = tuple(item["id"] for item in STRONG_RULES)
IMPLEMENTED_CHAIN = {
    "emotion": "PAD 连续情感模型",
    "desire": "情感驱动欲望",
    "game": "有限理性博弈",
    "decision": "LLM-driven",
    "learning": "经验回放",
}
BASE_ARCHITECTURE = (
    ("perception", "运行时局部观察"),
    ("memory", "运行时有限状态"),
    ("goal", "运行时目标与路线"),
    ("decision", "运行时规则决策"),
    ("action", "SUMO striping"),
)
AGENT_ARCHITECTURE_ORDER = (
    "perception", "memory", "desire", "goal", "game", "decision", "action", "learning",
)
PROFILE_PARAMETERS = {
    "free_walking_speed": {"label": "自由行走速度", "min": 1.0, "max": 1.6, "floor": 0.5, "ceiling": 2.5},
    "perception_radius": {"label": "感知半径", "min": 6.0, "max": 18.0, "floor": 2.0, "ceiling": 30.0},
    "crowding_tolerance": {"label": "拥挤容忍度", "min": 0.0, "max": 1.0, "floor": 0.0, "ceiling": 1.0},
    "following_tendency": {"label": "跟随倾向", "min": 0.0, "max": 1.0, "floor": 0.0, "ceiling": 1.0},
    "authority_compliance": {"label": "指引遵从倾向", "min": 0.0, "max": 1.0, "floor": 0.0, "ceiling": 1.0},
}
ENVIRONMENT_FACTORS = {
    "density": {"name": "局部人群密度", "method": "运行时邻域密度统计"},
    "flow": {"name": "出入口流率", "method": "运行时出发到达计数"},
}
ENVIRONMENT_RESPONSES = {
    "density-feedback": {"name": "密度—速度反馈", "theory": "SUMO striping 行人模型", "method": "SUMO 原生拥挤交互"},
    "capacity-feedback": {"name": "路网容量反馈", "theory": "SUMO 路网容量约束", "method": "路网通行与排队推进"},
}
class ModelingRequirementsError(ValueError):
    pass


ENVIRONMENT_CATALOG_PATH = PROJECT_ROOT / "config/environment_model_catalog.json"


def _load_environments() -> dict[str, dict[str, Any]]:
    data = json.loads(ENVIRONMENT_CATALOG_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("models"), list):
        raise ModelingRequirementsError("environment model catalog is invalid")
    return {str(item["location_id"]): item for item in data["models"] if isinstance(item, dict)}


ENVIRONMENTS = _load_environments()


def executable_catalog() -> list[dict[str, Any]]:
    catalog = [item for item in algorithm_catalog() if item.get("implemented")]
    catalog.extend([
        {"model": "individual", "module": module, "method": method,
         "module_path": "crowdsim.core.simulation_runtime", "implemented": True}
        for module, method in BASE_ARCHITECTURE
    ])
    catalog.append({
        "model": "environment", "module": "runtime", "method": "SUMO 路网拓扑",
        "module_path": "crowdsim.core.simulation_runtime", "implemented": True,
    })
    catalog.extend([
        {"model": "environment", "module": "factor", "method": item["method"],
         "module_path": "crowdsim.core.simulation_runtime", "implemented": True}
        for item in ENVIRONMENT_FACTORS.values()
    ])
    catalog.extend([
        {"model": "environment", "module": "response", "method": item["method"],
         "module_path": "crowdsim.core.simulation_runtime", "implemented": True}
        for item in ENVIRONMENT_RESPONSES.values()
    ])
    catalog.extend([
        {"model": "rule", "module": "runtime", "rule_id": item["id"],
         "method": item["default_method"], "name": item["name"],
         "summary": item["summary"], "runtime_consumed": True,
         "runtime_component": item["methods"][0]["runtime_component"],
         "module_path": item["methods"][0]["module_path"], "implemented": True}
        for item in STRONG_RULES
    ])
    return list({(item['model'], item['module'], item['method']): item for item in catalog}.values())


def environment_candidates(requirement: dict[str, Any]) -> list[dict[str, Any]]:
    spatial = requirement.get("spatial_scope", {})
    location_id = str(spatial.get("location_id") or "")
    location_name = str(spatial.get("name") or "").strip()
    event_category = str(requirement.get("scenario", {}).get("event_category", {}).get("id") or "")
    problem_type = requirement.get("research", {}).get("problem_type")
    population_total = requirement.get("population", {}).get("total")
    results = []
    for candidate in ENVIRONMENTS.values():
        if not candidate.get("during_runtime_ready"):
            continue
        config = PROJECT_ROOT / str(candidate.get("sumo_config") or "")
        if not config.is_file():
            continue
        aliases = {str(item) for item in candidate.get("location_aliases") or []}
        location_match = (candidate.get("location_id") == location_id if location_id
                          else bool(location_name) and location_name in aliases)
        if not location_match:
            continue
        reasons = ["研究地点与环境模型的场景资产一致"]
        score = 60
        if event_category and event_category in set(candidate.get("event_categories") or []):
            score += 15; reasons.append("活动类型在该环境模型的适用目录中")
        if isinstance(population_total, int) and candidate.get("population_min", 0) <= population_total <= candidate.get("population_max", 0):
            score += 15; reasons.append("计划人群规模位于模型可执行范围")
        if problem_type in set(candidate.get("research_problem_types") or []):
            score += 10; reasons.append("支持当前研究问题类型")
        results.append({
            "environment_model_id": candidate["environment_model_id"],
            "version": candidate["version"], "name": candidate["name"],
            "location_id": candidate["location_id"], "network_method": candidate["network_method"],
            "runtime_ready": True, "match_score": score,
            "match_dimensions": {"location": location_match, "event_category": event_category in set(candidate.get("event_categories") or []),
                                 "population": isinstance(population_total, int) and candidate.get("population_min", 0) <= population_total <= candidate.get("population_max", 0),
                                 "research_problem_type": problem_type in set(candidate.get("research_problem_types") or [])},
            "match_reason": "；".join(reasons),
            "entrances": deepcopy(candidate["entrances"]), "exits": deepcopy(candidate["exits"]),
            "factors": deepcopy(ENVIRONMENT_FACTORS), "responses": deepcopy(ENVIRONMENT_RESPONSES),
            "source": "config/environment_model_catalog.json",
        })
    return sorted(results, key=lambda item: (-int(item["match_score"]), item["environment_model_id"]))


def rule_recommendations(requirement: dict[str, Any]) -> list[dict[str, Any]]:
    recommendations = []
    scenario = requirement.get("scenario", {})
    problem_type = requirement.get("research", {}).get("problem_type")
    if scenario.get("hotspot_start_seconds") is not None:
        recommendations.append({
            "id": "hotspot-activity-schedule", "name": "热点活动时序", "required": True,
            "reason": "需求包含热点活动开始时间，需要按确认快照驱动等待、观景与离场阶段。",
            "effect_if_omitted": "活动时序不会进入个体路线阶段切换。",
            "runtime_method": "ActivityPlanner + HotspotRouteChoice", "implemented": True,
        })
    if problem_type == 2:
        recommendations.append({
            "id": "intervention-action-window", "name": "干预动作时窗", "required": True,
            "reason": "决策类研究需要约束控制动作的入口、开始时间与结束时间。",
            "effect_if_omitted": "策略动作不能安全绑定到有效控制时段。",
            "runtime_method": "InterventionExecutor + RuntimeCommandQueue", "implemented": True,
        })
    if scenario.get("hotspot_start_seconds") is not None:
        recommendations.append({"id": "congestion-route-choice", "name": "拥堵感知入口重选", "required": False,
            "reason": "聚集场景允许个体比较入口预期通行时间，依据画像选择是否绕行。",
            "effect_if_omitted": "保持初始分配的热点路线，不计算替代入口。",
            "runtime_method": "HotspotRouteChoice", "implemented": True})
    return recommendations


def _validate_emotion_state(emotion_state: Any, executable_methods: set[tuple[str, str]]) -> None:
    """Validate the state-only emotion block supplied with an enabled emotion capability."""
    if not isinstance(emotion_state, dict):
        raise ModelingRequirementsError("emotionState must be an object")
    method = emotion_state.get("method")
    if not isinstance(method, str) or not method.strip():
        raise ModelingRequirementsError("emotionState.method is required when emotion capability is enabled")
    if ("emotion", method) not in executable_methods:
        raise ModelingRequirementsError(f"emotion method is not executable: {method}")
    participates = emotion_state.get("participates_in_operation")
    if not isinstance(participates, bool):
        raise ModelingRequirementsError("emotionState.participates_in_operation must be a boolean")


def default_modeling_requirements(requirement: dict[str, Any]) -> dict[str, Any]:
    problem_type = requirement.get("research", {}).get("problem_type")
    crowd_roles = requirement.get("population", {}).get("distributions", {}).get("crowd_role") or []
    return {
        "schema_version": 1,
        "status": "draft",
        "decisions": {section: {"status": "pending", "fingerprint": "", "source": ""}
                      for section in ("individual", "environment", "rules")},
        "individual": {
            "agent_types": [{
                "id": str(row.get("code") or row.get("id") or f"crowd-{index + 1}"),
                "name": str(row.get("label") or f"人群类型 {index + 1}"),
                "proportion": float(row.get("percent") or 0),
                "capabilities": {"emotion": False, "game": False, "learning": False},
                "architecture": [{"id": module, "method": method} for module, method in BASE_ARCHITECTURE],
                "attributes": deepcopy(PROFILE_PARAMETERS),
            } for index, row in enumerate(crowd_roles)],
            "system_agent_types": [] if problem_type == 1 else [],
        },
        "environment": {
            "query": {
                "location_id": requirement.get("spatial_scope", {}).get("location_id") or "",
                "location_name": requirement.get("spatial_scope", {}).get("name") or "",
                "event_category": requirement.get("scenario", {}).get("event_category", {}).get("id") or "",
                "population_total": requirement.get("population", {}).get("total") or 0,
            },
            "candidates": environment_candidates(requirement),
            "selected_environment_model_id": "",
            "factors": ["density", "flow"],
            "responses": ["density-feedback", "capacity-feedback"],
        },
        "rules": {
            "required_rule_ids": list(REQUIRED_RULE_IDS),
            "rule_methods": deepcopy(default_rule_methods()),
            "recommended_rule_ids": [item["id"] for item in rule_recommendations(requirement)],
            "accepted_recommended_rule_ids": [],
            "rejected_recommended_rule_ids": [],
            "recommendations": rule_recommendations(requirement),
            "decision_period_seconds": 2.0,
            "visual_state": asdict(CrowdVisualPolicy.load()),
        },
    }


def validate_modeling_requirements(requirement: dict[str, Any], raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ModelingRequirementsError("modeling requirements schema_version must be 1")
    draft = deepcopy(raw)
    for section in ("individual", "environment", "rules"):
        decision = (draft.get("decisions") or {}).get(section) or {}
        if decision.get("status") not in {"accepted", "modified"} or not decision.get("source"):
            raise ModelingRequirementsError(f"{section} modeling requirements require explicit user confirmation")
        expected_section = deepcopy(draft.get(section) or {})
        if section == "environment":
            expected_section.pop("candidates", None)
        try:
            confirmed_section = json.loads(decision.get("fingerprint", ""))
        except (ValueError, TypeError):
            raise ModelingRequirementsError(f"{section} confirmation fingerprint is invalid") from None
        if confirmed_section != expected_section:
            raise ModelingRequirementsError(f"{section} changed after confirmation")
    expected = requirement.get("population", {}).get("distributions", {}).get("crowd_role") or []
    agents = draft.get("individual", {}).get("agent_types")
    if not isinstance(agents, list) or not agents:
        raise ModelingRequirementsError("individual agent types are required")
    expected_ids = {str(row.get("code") or row.get("id")) for row in expected}
    if {str(agent.get("id")) for agent in agents if isinstance(agent, dict)} != expected_ids:
        raise ModelingRequirementsError("agent types must match the confirmed crowd roles")
    methods = {(str(item["module"]), str(item["method"])) for item in executable_catalog()
               if item["model"] == "individual"}
    for agent in agents:
        capabilities = agent.get("capabilities") or {}
        architecture = [item for item in agent.get("architecture") or [] if item.get("selected") is not False]
        configured = {str(item.get("id")): str(item.get("method") or "") for item in architecture}
        for module, _ in BASE_ARCHITECTURE:
            if module not in configured:
                raise ModelingRequirementsError(f"agent {agent.get('name')} is missing {module}")
        # The emotion method is carried by emotionState, never by an architecture module.
        if "emotion" in configured:
            raise ModelingRequirementsError("emotion method is selected via emotionState, not the architecture")
        emotion_state = agent.get("emotionState") or {}
        if capabilities.get("emotion"):
            _validate_emotion_state(emotion_state, methods)
        # Optional architecture modules must exactly reflect the user's confirmed capabilities.
        desire_expected = capabilities.get("emotion") is True and emotion_state.get("participates_in_operation") is True
        if ("desire" in configured) != desire_expected:
            raise ModelingRequirementsError("desire module must match emotionState.participates_in_operation")
        if ("game" in configured) != (capabilities.get("game") is True):
            raise ModelingRequirementsError("game module must match game capability")
        if ("learning" in configured) != (capabilities.get("learning") is True):
            raise ModelingRequirementsError("learning module must match learning capability")
        ordered = [str(item.get("id")) for item in architecture]
        if len(ordered) != len(set(ordered)) or ordered != [key for key in AGENT_ARCHITECTURE_ORDER if key in ordered]:
            raise ModelingRequirementsError("architecture order must follow perception-memory-desire-goal-game-decision-action-learning")
        attributes = agent.get("attributes") or {}
        if set(attributes) != set(PROFILE_PARAMETERS):
            raise ModelingRequirementsError("agent attributes must match executable profile parameters")
        for key, bounds in PROFILE_PARAMETERS.items():
            lo, hi = attributes[key].get("min"), attributes[key].get("max")
            if (type(lo) not in (int, float) or type(hi) not in (int, float)
                    or not bounds["floor"] <= lo <= hi <= bounds["ceiling"]):
                raise ModelingRequirementsError(f"{key} bounds are invalid")
        for module, method in configured.items():
            if (module, method) not in methods:
                raise ModelingRequirementsError(f"algorithm is not executable: {module}.{method}")
    environment = draft.get("environment") or {}
    selected = str(environment.get("selected_environment_model_id") or "")
    candidates = {item["environment_model_id"]: item for item in environment_candidates(requirement)}
    if not selected or selected not in candidates:
        raise ModelingRequirementsError("an executable environment model must be selected")
    for kind in ("entrances", "exits"):
        ports = environment.get(kind, candidates[selected].get(kind, []))
        registered = {port["id"]: port for port in candidates[selected].get(kind, [])}
        if not isinstance(ports, list) or len(ports) != len(registered) or {port.get("id") for port in ports} != set(registered):
            raise ModelingRequirementsError(f"{kind} must match registered environment ports")
        for port in ports:
            capacity = port.get("capacity_per_minute")
            if port.get("edge_id") != registered[port["id"]]["edge_id"] or port.get("state") not in {"开放", "限流", "关闭"}:
                raise ModelingRequirementsError("port must reference a registered edge and valid state")
            if capacity is not None and (type(capacity) not in (int, float) or not 0 < capacity <= 60000):
                raise ModelingRequirementsError("port capacity must be positive and finite")
            if port.get("state") == "限流" and capacity is None:
                raise ModelingRequirementsError("limited port requires an explicit capacity")
    density_response = environment.get("density_response") or {"method": "SUMO 原生拥挤交互"}
    if density_response.get("method") not in {"SUMO 原生拥挤交互", "基本图模型"}:
        raise ModelingRequirementsError("density response method is not executable")
    if density_response["method"] == "基本图模型":
        critical, ratio = density_response.get("critical_density"), density_response.get("minimum_speed_ratio")
        if type(critical) not in (int, float) or not 0 < critical <= 20 or type(ratio) not in (int, float) or not 0 <= ratio <= 1:
            raise ModelingRequirementsError("fundamental diagram parameters are invalid")
    if draft.get("individual", {}).get("system_agent_types"):
        raise ModelingRequirementsError("autonomous system agents are not implemented; signal and policy are runtime services")
    factors = list(environment.get("factors") or [])
    responses = list(environment.get("responses") or [])
    unknown_factors = set(factors) - set(ENVIRONMENT_FACTORS)
    unknown_responses = set(responses) - set(ENVIRONMENT_RESPONSES)
    if unknown_factors:
        raise ModelingRequirementsError(f"environment factors are not executable: {', '.join(sorted(unknown_factors))}")
    if unknown_responses:
        raise ModelingRequirementsError(f"environment responses are not executable: {', '.join(sorted(unknown_responses))}")
    if set(factors) != set(ENVIRONMENT_FACTORS) or set(responses) != set(ENVIRONMENT_RESPONSES):
        raise ModelingRequirementsError("runtime requires density, flow, density-feedback and capacity-feedback")
    rules = draft.get("rules") or {}
    if set(rules.get("required_rule_ids") or []) != set(REQUIRED_RULE_IDS):
        raise ModelingRequirementsError("all required runtime rules must be present")
    rule_methods = rules.get("rule_methods")
    if rule_methods is None:
        rule_methods = {}
    if not isinstance(rule_methods, dict):
        raise ModelingRequirementsError("rules.rule_methods must be an object")
    unknown_rule_methods = set(rule_methods) - set(REQUIRED_RULE_IDS)
    if unknown_rule_methods:
        raise ModelingRequirementsError(f"unknown strong rules: {', '.join(sorted(unknown_rule_methods))}")
    for rule_id in REQUIRED_RULE_IDS:
        method = rule_methods.get(rule_id)
        if method is None:
            continue  # omitted -> catalogue default executable method
        if not implemented_rule_method(rule_id, method):
            raise ModelingRequirementsError(f"rule method is not executable: {rule_id}.{method}")
    recommendations = rule_recommendations(requirement)
    recommendation_ids = {item["id"] for item in recommendations}
    configured_recommendations = set(rules.get("recommended_rule_ids") or [])
    accepted_recommendations = set(rules.get("accepted_recommended_rule_ids") or [])
    rejected_recommendations = set(rules.get("rejected_recommended_rule_ids") or [])
    if (configured_recommendations != recommendation_ids or accepted_recommendations & rejected_recommendations
            or accepted_recommendations | rejected_recommendations != recommendation_ids):
        raise ModelingRequirementsError("each recommended rule must be explicitly accepted or rejected")
    if any(item["required"] and item["id"] not in accepted_recommendations for item in recommendations):
        raise ModelingRequirementsError("required scenario rules cannot be omitted")
    policy = CrowdVisualPolicy(**dict(rules.get("visual_state") or {}))
    period = rules.get("decision_period_seconds")
    if isinstance(period, bool) or not isinstance(period, (int, float)) or period <= 0:
        raise ModelingRequirementsError("decision period must be positive")
    draft["rules"]["visual_state"] = asdict(policy)
    draft["status"] = "confirmed"
    return draft


def _compile_agent(agent: dict[str, Any]) -> dict[str, Any]:
    architecture = [{**deepcopy(item), "selected": item.get("selected") is not False}
                    for item in agent["architecture"]]
    return {
        "id": str(agent["id"]), "name": str(agent["name"]),
        "proportion": float(agent.get("proportion") or 0), "enabled": True,
        "attributes": deepcopy(agent.get("attributes") or PROFILE_PARAMETERS),
        "capabilities": deepcopy(agent.get("capabilities") or {}),
        "emotionState": deepcopy(agent.get("emotionState") or {}),
        "architecture": architecture,
    }


def compile_artificial_society(requirement_record: dict[str, Any], modeling: dict[str, Any]) -> dict[str, Any]:
    requirement = requirement_record["requirement"]
    modeling = validate_modeling_requirements(requirement, modeling)
    environment_id = modeling["environment"]["selected_environment_model_id"]
    environment = next(item for item in ENVIRONMENTS.values() if item["environment_model_id"] == environment_id)
    policy = deepcopy(modeling["rules"]["visual_state"])
    population = requirement["population"]
    selected_rule_methods = modeling["rules"].get("rule_methods") or {}
    rules = []
    for entry in STRONG_RULES:
        method_record = resolve_rule_method(entry["id"], selected_rule_methods.get(entry["id"]))
        rules.append({
            "id": entry["id"], "category": entry["category"], "name": entry["name"],
            "method": method_record["label"], "summary": entry["summary"],
            "runtime_component": method_record["runtime_component"],
            "module_path": method_record["module_path"],
            "selected": True, "status": "runtime_consumed",
        })
    accepted_recommendations = set(modeling["rules"].get("accepted_recommended_rule_ids") or [])
    recommendations = [{**item, "category": "建议规则", "selected": True}
                       for item in rule_recommendations(requirement) if item["id"] in accepted_recommendations]
    payload = {
        "schema_version": 2, "source": "manual",
        "requirement_id": requirement_record["requirement_id"],
        "requirement_fingerprint": requirement_record["fingerprint"],
        "overview": {"title": requirement["project"]["title"], "description": requirement["project"]["description"]},
        "individual_model": {
            "title": "个体模型", "description": "按已确认建模需求构建异质人群智能体。",
            "population_total": population["total"], "distributions": deepcopy(population["distributions"]),
            "profile_parameters": deepcopy(PROFILE_PARAMETERS),
            "module_catalog": [{"id": module, "name": name} for module, name in (
                ("perception", "环境感知"), ("memory", "记忆与认知"), ("emotion", "情感状态"),
                ("desire", "欲望生成"), ("goal", "目标管理"), ("game", "博弈推理"),
                ("decision", "行为决策"), ("action", "动作执行"), ("learning", "适应与学习"),
            )],
            "roles": [{"id": "pedestrian", "selected": True}],
            "role_contract": "autonomous-agent-v1",
            "runtime_components": ["information-delivery", "intervention-executor"],
            "agent_types": [_compile_agent(agent) for agent in modeling["individual"]["agent_types"]],
        },
        "environment_model": {
            "title": "环境模型", "description": "绑定真实场景资源并装配环境状态与响应。",
            "environment_model_id": environment_id, "environment_model_version": environment["version"],
            "scenario_binding": {"location_id": environment["location_id"], "name": environment["name"]},
            "layers": [{"id": layer, "selected": True} for layer in ("topology", "dynamic", "information", "intervention")],
            "spatial_structure": {
                "network_name": environment["name"], "network_method": environment["network_method"],
                "sumo_config": environment["sumo_config"], "road_network_url": environment["road_network_url"],
                "entrances": deepcopy(modeling["environment"].get("entrances", environment["entrances"])),
                "exits": deepcopy(modeling["environment"].get("exits", environment["exits"])),
                "capacity_contract": "directional-token-bucket-v1",
                "density_response": deepcopy(modeling["environment"].get("density_response") or {"method": "SUMO 原生拥挤交互"}),
                "interior_edges": deepcopy(environment.get("interior_edges", [])),
                "factors": [{"id": item, **ENVIRONMENT_FACTORS[item], "selected": True}
                            for item in modeling["environment"].get("factors") or []],
                "responses": [{"id": item, **ENVIRONMENT_RESPONSES[item], "selected": True,
                               **({"theory": "线性密度—速度基本图", "method": "基本图模型", "parameter_status": "engineering_assumption_not_calibrated"}
                                  if item == "density-feedback" and modeling["environment"].get("density_response", {}).get("method") == "基本图模型" else {})}
                              for item in modeling["environment"].get("responses") or []],
            },
        },
        "rule_model": {
            "title": "规则模型", "description": "注入仿真运行所需的强约束规则。",
            "rules": [
                *[{**rule, "status": "runtime_consumed"} for rule in rules],
                *[{**item, "summary": item.get("reason") or "后端仿真运行时执行", "status": "runtime_consumed"}
                  for item in recommendations],
            ],
            "runtime_parameters": {"decision_period_seconds": modeling["rules"]["decision_period_seconds"],
                                   "congestion_route_choice": "congestion-route-choice" in accepted_recommendations,
                                   "visual_state": policy},
        },
    }
    ArtificialSocietySpec.parse(payload)
    return payload


def build_artificial_society(payload: dict[str, Any], workspace_id: str,
                             *, requirements: RequirementRepository | None = None,
                             models: ArtificialSocietyRepository | None = None) -> dict[str, Any]:
    conversation = payload.get("conversationId")
    snapshot = payload.get("snapshotId")
    if not isinstance(conversation, str) or workspace_id != f"during-{conversation}" or not snapshot:
        raise ModelingRequirementsError("conversation and snapshot do not match the workspace")
    requirements = requirements or RequirementRepository()
    models = models or ArtificialSocietyRepository(requirements=requirements)
    record = requirements.load(payload.get("requirementId"))
    if record["fingerprint"] != payload.get("requirementFingerprint"):
        raise ModelingRequirementsError("requirement fingerprint mismatch")
    compiled = compile_artificial_society(record, payload.get("modelingRequirements"))
    spec = ArtificialSocietySpec.parse(compiled)
    for path in sorted(models.root.glob("asm-*.json")):
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("requirement_id") == record["requirement_id"] and existing.get("fingerprint") == f"sha256:{spec.fingerprint}":
            model = models.load(existing["model_id"])
            break
    else:
        model = models.create(spec)
    experiment_config = {
        "research": deepcopy(record["requirement"].get("research") or {}),
        "experiment": deepcopy(record["requirement"].get("experiment") or {}),
        "observation": deepcopy(record["requirement"].get("observation") or {}),
        "modelId": model["model_id"],
        "modelFingerprint": model["fingerprint"],
    }
    experiment_canonical = json.dumps(experiment_config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    experiment_config_id = f"exp-config-{hashlib.sha256(experiment_canonical.encode()).hexdigest()[:16]}"
    execution_payload = {
        "conversationId": conversation, "planSnapshotId": snapshot,
        "requirementId": record["requirement_id"], "requirementFingerprint": record["fingerprint"],
        "modelId": model["model_id"], "modelFingerprint": model["fingerprint"],
        "environmentModelId": compiled["environment_model"]["environment_model_id"],
        "environmentModelVersion": compiled["environment_model"]["environment_model_version"],
        "experimentConfigId": experiment_config_id,
        "status": "READY",
    }
    canonical = json.dumps(execution_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        **model,
        "modeling_requirements": deepcopy(payload["modelingRequirements"]),
        "execution_manifest": {**execution_payload, "fingerprint": f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"},
        "algorithm_catalog": executable_catalog(),
        "build_steps": [
            {"id": "validate", "name": "建模需求校验", "status": "completed"},
            {"id": "individual", "name": "个体模型构建", "status": "completed"},
            {"id": "environment", "name": "环境模型绑定", "status": "completed"},
            {"id": "rules", "name": "规则模型装配", "status": "completed"},
            {"id": "manifest", "name": "推演清单生成", "status": "completed"},
        ],
    }
