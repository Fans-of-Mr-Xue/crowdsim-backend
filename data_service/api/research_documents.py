"""Original resources share the same streaming import and library API."""
from .knowledge_documents import register_document_routes
from ..schemas.research_documents import PROFILE


def register_research_routes(app, database, *, model=None):
    return register_document_routes(app, database, PROFILE, model=model)
