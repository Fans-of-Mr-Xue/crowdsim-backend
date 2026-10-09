"""Regulation routes use their own schema and model prompt profile."""
from .knowledge_documents import register_document_routes
from ..schemas.regulations import PROFILE


def register_regulation_routes(app, database, *, model=None):
    return register_document_routes(app, database, PROFILE, model=model)
