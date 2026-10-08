"""F-00 plus per-group, per-seed counterfactual task endpoints."""

from __future__ import annotations

import re

from .common import Request, response


def handle(request: Request):
    if request.path == "/experiments":
        if request.method == "POST":
            return request.mutation(lambda: (201, request.app.create_experiment(request.user, request.workspace, request.body)))
        if request.method == "GET":
            return response(request.app.repo.list("experiments", request.user, request.workspace))
    match = re.fullmatch(r"/experiments/([A-Za-z0-9_-]+)(?:/(start|cancel|events|runs))?", request.path)
    if match:
        item_id, action = match.groups()
        if request.method == "GET" and not action:
            return response(request.app.repo.get("experiments", item_id, request.user, request.workspace))
        if request.method == "GET" and action == "events":
            after = int((request.query.get("after") or [0])[0])
            if after < 0:
                raise ValueError("after must be nonnegative")
            return response(request.app.repo.events("experiments", item_id, request.user, request.workspace, after))
        if request.method == "GET" and action == "runs":
            request.app.repo.get("experiments", item_id, request.user, request.workspace)
            items = [run for run in request.app.repo.list("runs", request.user, request.workspace, limit=1000)
                     if run.get("experimentId") == item_id]
            return response({"experimentId": item_id, "runs": items})
        if request.method == "POST" and action in {"start", "cancel"}:
            operation = request.app.start if action == "start" else request.app.cancel
            status = 202 if action == "start" else 200
            return request.mutation(lambda: (status, operation("experiments", item_id, request.user, request.workspace)))
    return None
