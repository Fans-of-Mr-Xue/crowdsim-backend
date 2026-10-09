"""Canonical original resources: deduplicate bytes and retain durable asset-sync requests."""
from gridfs import GridFSBucket
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .knowledge_documents import KnowledgeDocumentRepository, now
from .datasets import DatasetConflictError
from ..schemas.research_documents import PROFILE
from ..services.research_documents import separate_regulations


class ResearchDocumentRepository(KnowledgeDocumentRepository):
    def __init__(self, database):
        super().__init__(database, PROFILE)
        self.bucket = GridFSBucket(database, bucket_name="emergency_plan_files")

    def initialize(self):
        super().initialize()
        self.collection.create_index("sha256", unique=True)
        self.collection.create_index([("asset_sync_status", 1), ("parse_status", 1)])

    def public(self, doc):
        result = super().public(doc)
        if result is None:
            return None
        result["derived_assets"] = []
        for collection, kind in (("emergency_plans", "plan"), ("regulations", "law")):
            for asset in self.database[collection].find({"source_id": doc["_id"], "is_deleted": {"$ne": True}}, {"name": 1, "parse_status": 1, "review_status": 1}):
                result["derived_assets"].append({"id": asset["_id"], "kind": kind, **{key: asset.get(key, "") for key in ("name", "parse_status", "review_status")}})
        return result

    def create(self, source, filename, size, checksum, **options):
        # Imports can arrive before the background worker finishes startup.
        self.collection.create_index("sha256", unique=True)
        existing = self.collection.find_one({"sha256": checksum})
        if existing is None:
            try:
                return self.store_original(source, filename, size, checksum, **options)
            except DuplicateKeyError:
                existing = self.collection.find_one({"sha256": checksum})
        if existing.get("is_deleted"):
            existing = self.collection.find_one_and_update({"_id": existing["_id"], "revision": existing["revision"]},
                {"$set": {"is_deleted": False, "parse_status": "queued", "parse_error": "", "asset_sync_status": "pending", "updated_at": now(),
                          **({"llm_provider": options["llm_provider"]} if options.get("llm_provider") else {})},
                 "$inc": {"revision": 1}}, return_document=ReturnDocument.AFTER)
            if existing is None:
                raise DatasetConflictError("原始资料正在更新，请重试")
        return {**self.public(existing), "reused": True}

    def update(self, identifier, payload, revision):
        return super().update(identifier, separate_regulations(dict(payload)), revision)

    def finish(self, identifier, run_id, **options):
        # Set the outbox before releasing the parsing lease, so a crash cannot lose the sync request.
        if options.get("metadata") is not None and not options.get("error"):
            options["metadata"] = separate_regulations(dict(options["metadata"]))
            self.collection.update_one({"_id": identifier, "run_id": run_id, "parse_status": "parsing", "is_deleted": {"$ne": True}},
                                       {"$set": {"asset_sync_status": "pending", "asset_sync_error": ""}})
        return super().finish(identifier, run_id, **options)

    def delete(self, identifier, revision):
        super().delete(identifier, revision)
        for collection in ("emergency_plans", "regulations"):
            self.database[collection].update_many({"source_id": identifier, "is_deleted": {"$ne": True}},
                {"$set": {"is_deleted": True, "deletion_reason": "source", "updated_at": now()}, "$inc": {"revision": 1}})
