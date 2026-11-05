"""Dataset registration and timeline queries."""

from __future__ import annotations

import re

from .common import Request, response


def handle(request: Request):
    path = request.path
    if request.method == "GET" and (path == "/during-sources" or path.startswith("/during-sources/")):
        from postanalysis_api.services.during_archives import DuringArchiveReader
        user, _workspace = request.user, request.workspace
        reader = DuringArchiveReader(request.app.repo.db)
        return response(reader.catalog(user) if path == "/during-sources" else reader.report(path[len("/during-sources/"):], user))
    if path == "/datasets":
        if request.method == "POST":
            return request.mutation(lambda: (201, request.app.dataset_metadata(
                request.app.create_dataset(request.user, request.workspace, request.body))))
        if request.method == "GET":
            return response([request.app.dataset_metadata(item) for item in
                             request.app.repo.list("datasets", request.user, request.workspace)])
    match = re.fullmatch(r"/datasets/([A-Za-z0-9_-]+)(?:/(timeline|reconstruction))?", path)
    if match and request.method == "GET":
        if match.group(2) == "reconstruction":
            from postanalysis_api.services.distribution_targets import distribution_targets
            dataset = request.app.repo.get("datasets", match.group(1), request.user, request.workspace)
            return response(distribution_targets(dataset))
        if match.group(2) == "timeline":
            return response(request.app.timeline(match.group(1), request.user, request.workspace, request.query))
        return response(request.app.dataset_metadata(
            request.app.repo.get("datasets", match.group(1), request.user, request.workspace)))
    return None
