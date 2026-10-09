"""Emergency plan persistence profile over the shared document repository."""
from .knowledge_documents import KnowledgeDocumentRepository
from ..schemas.emergency_plans import PROFILE


class EmergencyPlanRepository(KnowledgeDocumentRepository):
    def __init__(self, database):
        super().__init__(database, PROFILE)
