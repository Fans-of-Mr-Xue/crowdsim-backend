"""Persistence endpoints for the post-review workspace's complete plan drafts."""

from __future__ import annotations

import re

from .common import Request, response


def handle(request: Request):
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
