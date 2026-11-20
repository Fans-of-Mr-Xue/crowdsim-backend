"""Computed result sections; incomplete jobs never return preview numbers."""

from __future__ import annotations

import re

from .common import Request, response


SECTIONS = {"metrics": "metrics", "comparison": "comparison", "causal-graph": "causalGraph",
            "observation": "observation", "intervention": "intervention", "mechanism": "mechanism"}


def handle(request: Request):
    artifact = re.fullmatch(r'/results/([A-Za-z0-9_-]+)/analysis-artifact', request.path)
    if artifact and request.method == 'GET':
        from postanalysis_api.workers.source_analysis import read_artifact
        from postanalysis_api.schemas.common import ApiError
        try:
            offset = int((request.query.get('offset') or [0])[0])
            limit = int((request.query.get('limit') or [65536])[0])
        except (ValueError, TypeError):
            raise ApiError('INVALID_QUERY', '读取偏移及长度必须为整数', 400) from None
        return response(read_artifact(request.app.repo, artifact.group(1), request.user, request.workspace,
                                      offset, limit))
    match = re.fullmatch(r"/results/([A-Za-z0-9_-]+)/(metrics|comparison|causal-graph|observation|intervention|mechanism)", request.path)
    if match and request.method == "GET":
        return response(request.app.result(match.group(1), request.user, request.workspace, SECTIONS[match.group(2)]))
    return None
