"""Regulation persistence profile over the shared document repository."""
from .knowledge_documents import KnowledgeDocumentRepository
from ..schemas.regulations import PROFILE


class RegulationRepository(KnowledgeDocumentRepository):
    def __init__(self, database):
        super().__init__(database, PROFILE)
