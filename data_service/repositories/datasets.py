"""Shared MongoDB dataset repository for HTTP clients and future importers."""
from __future__ import annotations

from datetime import datetime, timezone
import os
import re
from uuid import uuid4

from ..config import load_database_env
from ..schemas.datasets import validate_dataset
from pymongo import MongoClient, ReturnDocument


class DatasetConflictError(Exception):
    pass


class DatasetRepository:
    def __init__(self, database=None):
        self.client = None
        if database is None:
            load_database_env()
            options = dict(serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000)
            uri = os.environ.get("CROWDSIM_MONGO_URI")
            if uri:
                self.client = MongoClient(uri, **options)
            else:
                username = os.environ.get("CROWDSIM_MONGO_USERNAME")
                if username:
                    options.update(username=username, password=os.environ.get("CROWDSIM_MONGO_PASSWORD", ""),
                                   authSource=os.environ.get("CROWDSIM_MONGO_AUTH_SOURCE", "admin"))
                self.client = MongoClient(os.environ.get("CROWDSIM_MONGO_HOST", "127.0.0.1"),
                                          int(os.environ.get("CROWDSIM_MONGO_PORT", "27018")), **options)
            database = self.client[os.environ.get("CROWDSIM_MONGO_DATABASE", "crowdsim")]
        self.db = database
        self.datasets = database["datasets"]

    def close(self):
        if self.client is not None:
            self.client.close()

    def status(self):
        self.db.command("ping")
        names = self.db.list_collection_names()
        return {"connected": True, "database": self.db.name, "collection_count": len(names)}

    def smoke_test(self):
        key = "smoke-" + uuid4().hex
        collection = self.db["connection_tests"]
        try:
            result = collection.insert_one({"_id": key, "created_at": datetime.now(timezone.utc).isoformat()})
            read_ok = collection.find_one({"_id": key}) is not None
        finally:
            deleted = collection.delete_one({"_id": key}).deleted_count
        return {"write_ok": result.acknowledged, "read_ok": read_ok, "cleanup_ok": deleted == 1}

    @staticmethod
    def public(document):
        if document is None:
            return None
        return {"id": str(document["_id"]), **{key: value for key, value in document.items() if key != "_id"}}

    def list(self, *, page=1, page_size=20, search="", source=""):
        query = {}
        if source:
            query["source"] = source
        if search:
            query["name"] = {"$regex": re.escape(search), "$options": "i"}
        cursor = self.datasets.find(query, {"records": 0}).sort([("updated_at", -1), ("_id", 1)])
        items = [self.public(doc) for doc in cursor.skip((page - 1) * page_size).limit(page_size)]
        return {"items": items, "total": self.datasets.count_documents(query), "page": page, "page_size": page_size}

    def get(self, identifier):
        return self.public(self.datasets.find_one({"_id": identifier}))

    def create(self, payload):
        document = validate_dataset(payload)
        now = datetime.now(timezone.utc).isoformat()
        document.update(_id=uuid4().hex, created_at=now, updated_at=now, revision=1)
        self.datasets.insert_one(document)
        return self.public(document)

    def update(self, identifier, payload, revision):
        document = validate_dataset(payload)
        document["updated_at"] = datetime.now(timezone.utc).isoformat()
        updated = self.datasets.find_one_and_update(
            {"_id": identifier, "revision": revision}, {"$set": document, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER)
        if updated is None:
            if self.datasets.find_one({"_id": identifier}, {"_id": 1}) is None:
                raise KeyError(identifier)
            raise DatasetConflictError("数据已被其他用户修改，请刷新后重试")
        return self.public(updated)
