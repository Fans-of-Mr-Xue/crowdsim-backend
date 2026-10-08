"""Business rules shared by the five route groups."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from postanalysis_api.repositories.local import LocalRepository
from postanalysis_api.schemas.common import ApiError, integer, number
from postanalysis_api.schemas.datasets import validate_dataset
from postanalysis_api.schemas.experiments import validate_experiment, validate_simulation
from postanalysis_api.schemas.metrics import METRICS, catalog
from postanalysis_api.workers.executor import UnavailableExecutor


class PostAnalysisApp:
    def __init__(self, data_dir: str | Path, executor=None):
        self.repo = LocalRepository(data_dir)
        self.repo.reconcile_interrupted()
        self.executor = executor or UnavailableExecutor()

    def capabilities(self) -> dict:
        return {"contractVersion": "post-api/1.0", "simulation": self.executor.capabilities(),
                "maxGroups": 12, "maxRepeatsPerGroup": 50, "maxParallelismRequested": 8,
                "baselinePlanId": "F-00", "baselineSeedPolicy": "shared_prefix_per_group",
                "metricCatalog": catalog(), "storage": "local-json-and-jsonl"}

    @staticmethod
    def dataset_metadata(document: dict) -> dict:
        return {key: value for key, value in document.items() if key != "observations"}

    def create_dataset(self, user: str, workspace: str, body: dict) -> dict:
        data = validate_dataset(body)
        fingerprint = hashlib.sha256(json.dumps(data["observations"], sort_keys=True,
                                                 separators=(",", ":")).encode("utf-8")).hexdigest()
        document = self.repo.create("datasets", user, workspace,
                                    {**data, "version": fingerprint, "observationCount": len(data["observations"])})
        self.repo.event("datasets", document["id"], "imported", {"version": fingerprint})
        return document

    def timeline(self, item_id: str, user: str, workspace: str, query: dict) -> dict:
        dataset = self.repo.get("datasets", item_id, user, workspace)
        start = integer(int((query.get("fromSeconds") or [0])[0]), "fromSeconds", 0, 86400)
        end = integer(int((query.get("toSeconds") or [86400])[0]), "toSeconds", 0, 86400)
        if end < start:
            raise ApiError("TIME_ORDER_INVALID", "toSeconds 必须不早于 fromSeconds", field="toSeconds")
        offset = integer(int((query.get("offset") or [0])[0]), "offset", 0, 1000000)
        limit = integer(int((query.get("limit") or [500])[0]), "limit", 1, 1000)
        region = (query.get("regionId") or [None])[0]
        rows = [row for row in dataset["observations"] if start <= row["timeSeconds"] <= end
                and (region is None or row["regionId"] == region)]
        return {"datasetId": item_id, "datasetVersion": dataset["version"],
                "sourceKind": dataset["sourceKind"], "total": len(rows),
                "items": rows[offset:offset + limit], "nextOffset": offset + limit if offset + limit < len(rows) else None}

    def _dataset(self, item_id: str, user: str, workspace: str) -> dict:
        return self.repo.get("datasets", item_id, user, workspace)

    def _check_start_capability(self, task: dict) -> None:
        capability = self.executor.capabilities()
        if not capability.get("ready"):
            raise ApiError("CAPABILITY_UNAVAILABLE", "事后 SUMO 批量执行器尚未接入", 503)
        scenario = task["scenario"]
        if scenario["scenarioId"] not in capability.get("scenarioIds", []):
            raise ApiError("SCENARIO_UNSUPPORTED", "SUMO 未声明此场景", 422, "scenario.scenarioId")
        if scenario["modelId"] not in capability.get("modelIds", []):
            raise ApiError("MODEL_UNSUPPORTED", "SUMO 未声明此模型", 422, "scenario.modelId")
        if scenario["modelVersion"] not in capability.get("modelVersions", {}).get(scenario["modelId"], []):
            raise ApiError("MODEL_VERSION_MISMATCH", "SUMO 模型版本不匹配", 422, "scenario.modelVersion")
        if scenario["geometryVersion"] not in capability.get("geometryVersions", {}).get(scenario["scenarioId"], []):
            raise ApiError("GEOMETRY_VERSION_MISMATCH", "场景几何版本不匹配", 422, "scenario.geometryVersion")
        if scenario["ruleVersion"] not in capability.get("ruleVersions", []):
            raise ApiError("RULE_VERSION_MISMATCH", "规则版本不匹配", 422, "scenario.ruleVersion")
        if task.get("parallelism", 1) > capability.get("maxParallelism", 0):
            raise ApiError("RESOURCE_LIMIT_EXCEEDED", "并行度超过本机能力", 422, "parallelism")
        planned = (1 if "groups" not in task else max(group["repeatCount"] for group in task["groups"]) +
                   sum(group["repeatCount"] for group in task["groups"]))
        if planned > capability.get("maxTotalRuns", 0):
            raise ApiError("RESOURCE_LIMIT_EXCEEDED", "计划运行次数超过本机能力", 422, "groups")
        region_map = capability.get("regionMap", {})
        for region_id, region in region_map.items():
            if not isinstance(region, dict) or not region.get("cellIds"):
                raise ApiError("REGION_UNMAPPED", f"区域 {region_id} 缺少固定网格映射", 422)
            number(region.get("effectiveAreaM2"), f"regionMap.{region_id}.effectiveAreaM2", 1e-9, 1e9)
        for feature in task.get("pcFeatures", []):
            if feature["regionId"] != "global" and feature["regionId"] not in region_map:
                raise ApiError("REGION_UNMAPPED", "PC 特征区域尚无 SUMO 映射", 422, "pcFeatures.regionId")
        for group in task.get("groups", []):
            for action in group["interventions"]:
                if action["actionType"] not in capability.get("actionTypes", []):
                    raise ApiError("ACTION_UNSUPPORTED", "仿真器不支持此干预动作", 422, "actionType")
                if action["targetRegionId"] not in region_map:
                    raise ApiError("REGION_UNMAPPED", "目标区域尚无 SUMO 路网映射", 422, "targetRegionId")
                parameter_schema = capability.get("actionSchemas", {}).get(action["actionType"])
                if not isinstance(parameter_schema, dict):
                    raise ApiError("ACTION_SCHEMA_MISSING", "动作参数范围尚未由 SUMO 声明", 422, "actionType")
                unexpected = set(action["parameters"]) - set(parameter_schema)
                if unexpected:
                    raise ApiError("ACTION_PARAMETER_INVALID", "动作包含未声明参数", 422, "parameters")
                for name, specification in parameter_schema.items():
                    value = action["parameters"].get(name)
                    if value is None:
                        if specification.get("required"):
                            raise ApiError("ACTION_PARAMETER_INVALID", f"缺少动作参数 {name}", 422, f"parameters.{name}")
                        continue
                    if specification.get("type") == "number":
                        number(value, f"parameters.{name}", specification["minimum"], specification["maximum"])
                    elif specification.get("type") == "integer":
                        integer(value, f"parameters.{name}", specification["minimum"], specification["maximum"])
                    elif specification.get("type") == "enum":
                        if value not in specification.get("values", []):
                            raise ApiError("ACTION_PARAMETER_INVALID", f"动作参数 {name} 不在允许值中", 422, f"parameters.{name}")
                    else:
                        raise ApiError("ACTION_SCHEMA_MISSING", "动作参数类型未声明", 422, f"parameters.{name}")
        available = set(capability.get("metricIds", []))
        for metric in task["metricIds"]:
            if metric not in available:
                raise ApiError("METRIC_UNSUPPORTED", "运行器缺少指标所需数据", 422, "metricIds")

    def create_simulation(self, user: str, workspace: str, body: dict) -> dict:
        data = validate_simulation(body)
        dataset = self._dataset(data["datasetId"], user, workspace)
        item = self.repo.create("simulations", user, workspace,
                                {**data, "datasetVersion": dataset["version"], "status": "draft",
                                 "progress": {"planned": 1, "completed": 0, "failed": 0}, "resultId": None})
        self.repo.event("simulations", item["id"], "created")
        return item

    def create_experiment(self, user: str, workspace: str, body: dict) -> dict:
        data = validate_experiment(body)
        dataset = self._dataset(data["datasetId"], user, workspace)
        baseline_count = max(group["repeatCount"] for group in data["groups"])
        intervention_count = sum(group["repeatCount"] for group in data["groups"])
        item = self.repo.create("experiments", user, workspace,
                                {**data, "datasetVersion": dataset["version"], "status": "draft",
                                 "progress": {"baselinePlanned": baseline_count, "baselineCompleted": 0,
                                              "interventionPlanned": intervention_count, "interventionCompleted": 0,
                                              "failed": 0,
                                              "groups": [{"groupId": group["id"], "planned": group["repeatCount"],
                                                          "completed": 0, "failed": 0} for group in data["groups"]]},
                                 "resultId": None})
        self.repo.event("experiments", item["id"], "created")
        return item

    def start(self, kind: str, item_id: str, user: str, workspace: str) -> dict:
        item = self.repo.get(kind, item_id, user, workspace)
        if item["status"] != "draft":
            raise ApiError("INVALID_TASK_STATE", "只有草稿任务可以启动", 409)
        dataset = self._dataset(item["datasetId"], user, workspace)
        if dataset["version"] != item["datasetVersion"]:
            raise ApiError("DATASET_VERSION_MISMATCH", "数据集版本与任务创建时不同", 409)
        self._check_start_capability(item)
        updated = self.repo.update(kind, item_id, user, workspace, {"status": "queued"})
        self.repo.event(kind, item_id, "queued")
        try:
            self.executor.submit(item, lambda event: self.record_worker_event(kind, item_id, user, workspace, event))
        except Exception:
            self.repo.update(kind, item_id, user, workspace, {"status": "draft"})
            self.repo.event(kind, item_id, "submit_rejected")
            raise
        return self.repo.get(kind, item_id, user, workspace)

    def record_worker_event(self, kind: str, item_id: str, user: str, workspace: str, event: dict) -> None:
        allowed = {"running", "completed", "failed", "cancelled", "partial"}
        if event.get("status") not in allowed:
            raise ApiError("INVALID_WORKER_EVENT", "执行器状态无效", 422)
        task = self.repo.get(kind, item_id, user, workspace)
        transitions = {"queued": {"running", "failed", "cancelled"},
                       "running": {"running", "completed", "partial", "failed", "cancelled"},
                       "cancelling": {"cancelled", "failed"}}
        if event["status"] not in transitions.get(task["status"], set()):
            raise ApiError("INVALID_TASK_STATE", "执行器请求的状态转移无效", 409)
        changes = {"status": event["status"]}
        if "progress" in event:
            if not isinstance(event["progress"], dict):
                raise ApiError("INVALID_WORKER_EVENT", "进度必须是对象", 422)
            progress = {**task["progress"], **event["progress"]}
            for key, planned in (("baselineCompleted", progress.get("baselinePlanned")),
                                 ("interventionCompleted", progress.get("interventionPlanned")),
                                 ("completed", progress.get("planned"))):
                if key in progress and planned is not None:
                    integer(progress[key], f"progress.{key}", 0, planned)
            integer(progress.get("failed", 0), "progress.failed", 0, 1000000)
            if "groups" in progress:
                expected_groups = {group["id"]: group["repeatCount"] for group in task["groups"]}
                if not isinstance(progress["groups"], list) or {item.get("groupId") for item in progress["groups"]} != set(expected_groups):
                    raise ApiError("PROGRESS_MISMATCH", "进度组与实验方案不一致", 409)
                for group_progress in progress["groups"]:
                    planned = expected_groups[group_progress["groupId"]]
                    if group_progress.get("planned") != planned:
                        raise ApiError("PROGRESS_MISMATCH", "组计划次数不可更改", 409)
                    integer(group_progress.get("completed"), "progress.groups.completed", 0, planned)
                    integer(group_progress.get("failed"), "progress.groups.failed", 0, planned)
                    if group_progress["completed"] + group_progress["failed"] > planned:
                        raise ApiError("PROGRESS_MISMATCH", "组成功和失败次数超过计划", 409)
            changes["progress"] = progress
        if "resultId" in event:
            result = self.repo.get("results", event["resultId"], user, workspace)
            if result.get("datasetVersion") != task["datasetVersion"]:
                raise ApiError("RESULT_VERSION_MISMATCH", "结果和数据集版本不一致", 409)
            changes["resultId"] = result["id"]
        if event["status"] in {"completed", "partial"} and not changes.get("resultId"):
            raise ApiError("RESULT_NOT_READY", "结果封存前不可报告任务完成", 409)
        self.repo.update(kind, item_id, user, workspace, changes)
        self.repo.event(kind, item_id, event["status"], {key: value for key, value in event.items() if key != "status"})

    def cancel(self, kind: str, item_id: str, user: str, workspace: str) -> dict:
        item = self.repo.get(kind, item_id, user, workspace)
        if item["status"] not in {"draft", "queued", "running"}:
            raise ApiError("INVALID_TASK_STATE", "任务当前状态不可取消", 409)
        if item["status"] in {"queued", "running"}:
            self.executor.cancel(item_id)
            status = "cancelling"
        else:
            status = "cancelled"
        updated = self.repo.update(kind, item_id, user, workspace, {"status": status})
        self.repo.event(kind, item_id, status)
        return updated

    def result(self, experiment_id: str, user: str, workspace: str, section: str) -> dict:
        task = self.repo.get("experiments", experiment_id, user, workspace)
        if task["status"] not in {"completed", "partial"} or not task.get("resultId"):
            raise ApiError("RESULT_NOT_READY", "真实仿真结果尚未生成", 409)
        result = self.repo.get("results", task["resultId"], user, workspace)
        if result.get("datasetVersion") != task["datasetVersion"]:
            raise ApiError("RESULT_VERSION_MISMATCH", "结果与任务所用数据集版本不一致", 409)
        if section not in result:
            raise ApiError("ANALYSIS_UNAVAILABLE", "该分析尚无可信计算结果", 409)
        return {"experimentId": experiment_id, "resultId": result["id"], "section": section,
                "definitionVersion": result.get("definitionVersion"), "data": result[section]}

    def model_connection(self) -> dict:
        model = os.environ.get("CROWDSIM_POST_LLM_MODEL", "qwen3.8-flash-next")
        headers = {}
        token = os.environ.get("CROWDSIM_POST_LLM_API_KEY")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request("http://127.0.0.1:8800/v1/models", headers=headers)
        try:
            with urlopen(request, timeout=2) as response:
                payload = json.load(response)
            models = [str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict)]
            return {"endpoint": "http://127.0.0.1:8800", "model": model,
                    "displayName": "Qwen3.8-Flash-Next",
                    "status": "ready" if model in models else "model_unavailable", "availableModels": models}
        except HTTPError as exc:
            return {"endpoint": "http://127.0.0.1:8800", "model": model,
                    "displayName": "Qwen3.8-Flash-Next", "status": "http_error",
                    "httpStatus": exc.code, "availableModels": []}
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            return {"endpoint": "http://127.0.0.1:8800", "model": model,
                    "displayName": "Qwen3.8-Flash-Next", "status": "unreachable", "availableModels": []}
