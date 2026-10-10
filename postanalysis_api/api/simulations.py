"""Factual single-run task endpoints."""

from __future__ import annotations

import re

from .common import Request, response


def handle(request: Request):
    if request.path == "/simulations":
        if request.method == "POST":
            return request.mutation(lambda: (201, request.app.create_simulation(request.user, request.workspace, request.body)))
        if request.method == "GET":
            return response(request.app.repo.list("simulations", request.user, request.workspace))
    match = re.fullmatch(r"/simulations/([A-Za-z0-9_-]+)(?:/(start|cancel|events))?", request.path)
    if match:
        item_id, action = match.groups()
        if request.method == "GET" and not action:
            return response(request.app.repo.get("simulations", item_id, request.user, request.workspace))
        if request.method == "GET" and action == "events":
            after = int((request.query.get("after") or [0])[0])
            if after < 0:
                raise ValueError("after must be nonnegative")
            return response(request.app.repo.events("simulations", item_id, request.user, request.workspace, after))
        if request.method == "POST" and action in {"start", "cancel"}:
            operation = request.app.start if action == "start" else request.app.cancel
            status = 202 if action == "start" else 200
            return request.mutation(lambda: (status, operation("simulations", item_id, request.user, request.workspace)))
    return None
