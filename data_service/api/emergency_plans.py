"""Emergency-plan routes use their own schema and model prompt profile."""
from .knowledge_documents import register_document_routes
from ..schemas.emergency_plans import PROFILE


def register_plan_routes(app, database, *, model=None):
    return register_document_routes(app, database, PROFILE, model=model)
