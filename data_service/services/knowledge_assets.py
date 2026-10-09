"""Build derived knowledge queues from evidence-backed source classification."""
import asyncio
import logging

from pymongo.errors import PyMongoError

from ..repositories.datasets import DatasetConflictError
from ..repositories.knowledge_documents import KnowledgeDocumentRepository, now
from ..schemas.emergency_plans import PROFILE as PLAN
from ..schemas.regulations import PROFILE as LAW

from .research_documents import separate_regulations

logger = logging.getLogger(__name__)


class AssetSynchronizer:
    def __init__(self, sources):
        self.sources = sources
        self.targets = [("emergency_plan", KnowledgeDocumentRepository(sources.database, PLAN)),
                        ("regulation", KnowledgeDocumentRepository(sources.database, LAW))]

    def synchronize(self, source):
        types = separate_regulations(dict(source)).get("knowledge_types", [])
        for knowledge_type, repository in self.targets:
            if knowledge_type in types:
                repository.attach_source(source["_id"])
            else:
                # Reclassification removes obsolete automatic, unreviewed results only.
                repository.collection.update_many({"source_id": source["_id"], "import_origin": "automatic", "review_status": "pending", "is_deleted": {"$ne": True}},
                    {"$set": {"is_deleted": True, "deletion_reason": "classification", "updated_at": now()}, "$inc": {"revision": 1}})
        self.sources.collection.update_one({"_id": source["_id"], "revision": source["revision"], "parse_status": "parsed", "is_deleted": {"$ne": True}},
            {"$set": {"asset_sync_status": "synced", "asset_sync_error": ""}})

    async def run(self):
        while True:
            try:
                for _, repository in self.targets:
                    await asyncio.to_thread(repository.initialize)
                break
            except PyMongoError:
                await asyncio.sleep(3)
        while True:
            try:
                source = await asyncio.to_thread(self.sources.collection.find_one,
                    {"is_deleted": {"$ne": True}, "parse_status": "parsed", "asset_sync_status": "pending"})
                if source:
                    await asyncio.to_thread(self.synchronize, source)
                else:
                    await asyncio.sleep(1)
            except (PyMongoError, DatasetConflictError):
                logger.warning("Knowledge asset synchronization waiting for database/concurrent update")
                await asyncio.sleep(3)
            except KeyError:
                # A source can be deleted while its synchronization is in progress.
                await asyncio.sleep(1)
