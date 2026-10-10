"""Computed result sections; incomplete jobs never return preview numbers."""

from __future__ import annotations

import re

from .common import Request, response


SECTIONS = {"metrics": "metrics", "comparison": "comparison", "causal-graph": "causalGraph",
            "observation": "observation", "intervention": "intervention", "mechanism": "mechanism"}


def handle(request: Request):
    match = re.fullmatch(r"/results/([A-Za-z0-9_-]+)/(metrics|comparison|causal-graph|observation|intervention|mechanism)", request.path)
    if match and request.method == "GET":
        return response(request.app.result(match.group(1), request.user, request.workspace, SECTIONS[match.group(2)]))
    return None
