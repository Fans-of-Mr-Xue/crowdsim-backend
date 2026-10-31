"""F-00 plus per-group, per-seed counterfactual task endpoints."""

from __future__ import annotations

import re

from .common import Request, response


def handle(request: Request):
    run_match = re.fullmatch(r"/runs/([A-Za-z0-9_-]+)(?:/(trajectory|archive))?", request.path)
    if run_match and request.method == "GET":
        run_id, action = run_match.groups()
        request.app.repo.get("runs", run_id, request.user, request.workspace)
        if action == "trajectory":
            after = int((request.query.get("after") or [-1])[0])
            limit = int((request.query.get("limit") or [10])[0])
            if after < -1 or not 1 <= limit <= 20:
                raise ValueError("trajectory cursor/limit out of range")
            return response(request.app.trajectory(run_id, request.user, request.workspace, after, limit))
        if action == "archive":
            return response(request.app.repo.archive_entry(run_id, request.user, request.workspace))
        return response(request.app.repo.get("runs", run_id, request.user, request.workspace))
    if request.path == "/experiments":
        if request.method == "POST":
            return request.mutation(lambda: (201, request.app.create_experiment(request.user, request.workspace, request.body)))
        if request.method == "GET":
            return response(request.app.repo.list("experiments", request.user, request.workspace))
    match = re.fullmatch(r"/experiments/([A-Za-z0-9_-]+)(?:/(start|cancel|events|runs|runtime))?", request.path)
    if match:
        item_id, action = match.groups()
        if request.method == "GET" and not action:
            return response(request.app.repo.get("experiments", item_id, request.user, request.workspace))
        if request.method == "GET" and action == "runtime":
            return response(request.app.runtime("experiments", item_id, request.user, request.workspace))
        if request.method == "GET" and action == "events":
            after = int((request.query.get("after") or [0])[0])
            if after < 0:
                raise ValueError("after must be nonnegative")
            return response(request.app.repo.events("experiments", item_id, request.user, request.workspace, after))
        if request.method == "GET" and action == "runs":
            request.app.repo.get("experiments", item_id, request.user, request.workspace)
            items = request.app.repo.runs_for_experiment(item_id, request.user, request.workspace)
            return response({"experimentId": item_id, "runs": items})
        if request.method == "POST" and action in {"start", "cancel"}:
            operation = request.app.start if action == "start" else request.app.cancel
            status = 202 if action == "start" else 200
            return request.mutation(lambda: (status, operation("experiments", item_id, request.user, request.workspace)))
    return None
