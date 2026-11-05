"""Persistence endpoints for the post-review workspace's complete plan drafts."""

from __future__ import annotations

import re

from .common import Request, response


def handle(request: Request):
    execution_match = re.fullmatch(r"/review-plans/([A-Za-z0-9_-]+)/execution", request.path)
    if execution_match:
        from postanalysis_api.schemas.common import ApiError
        from postanalysis_api.services.execution_template import execution_template, execution_readiness
        reviewed = request.app.repo.get("review_plans", execution_match[1], request.user, request.workspace)
        if request.method == "GET":
            dataset = request.app.repo.get("datasets", reviewed["datasetId"], request.user, request.workspace)
            readiness = execution_readiness(dataset, reviewed["plan"])
            if not readiness["ready"]:
                return response({"execution": None, "readiness": readiness, "requiresConfirmation": False, "note": readiness["message"]})
            return response({"execution": execution_template(dataset, reviewed["plan"]), "readiness": readiness, "requiresConfirmation": True,
                             "note": "默认数值为可编辑工程测试建议；请逐组核对，未确认不会运行。"})
        if request.method == "POST":
            if set(request.body) != {"execution", "confirmed"} or request.body.get("confirmed") is not True:
                raise ApiError("EXECUTION_NOT_CONFIRMED", "请显式确认工程执行参数", 422)
            execution = request.body["execution"]
            if not isinstance(execution, dict):
                raise ApiError("VALIDATION_ERROR", "execution 必须为对象", 422)
            execution = {**execution, "reviewPlanId": reviewed["id"]}
            return request.mutation(lambda: (201, request.app.create_experiment(request.user, request.workspace, execution)))
    if request.path == "/review-plans":
        if request.method == "POST":
            return request.mutation(lambda: (201, request.app.create_review_plan(
                request.user, request.workspace, request.body)))
        if request.method == "GET":
            return response(request.app.repo.list("review_plans", request.user, request.workspace))
    match = re.fullmatch(r"/review-plans/([A-Za-z0-9_-]+)", request.path)
    if match and request.method == "GET":
        return response(request.app.repo.get("review_plans", match.group(1), request.user, request.workspace))
    return None
