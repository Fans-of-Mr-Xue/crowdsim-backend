"""Read-only local vLLM availability; credentials never enter this API."""

from __future__ import annotations

from .common import Request, response


def handle(request: Request):
    if request.method == "GET" and request.path == "/model-connections":
        return response(request.app.model_connection())
    return None
