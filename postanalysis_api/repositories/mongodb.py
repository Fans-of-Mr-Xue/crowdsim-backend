"""MongoDB persistence for post-analysis datasets, plans, runs and results."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from threading import RLock
from uuid import uuid4

from gridfs import GridFSBucket
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from data_service.config import load_database_env
from postanalysis_api.schemas.common import ApiError


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MongoRepository:
    """Repository with the LocalRepository contract and GridFS source uploads."""

    KINDS = {"datasets", "simulations", "experiments", "runs", "results", "review_plans"}
    COLLECTIONS = {"datasets": "postanalysis_datasets", "simulations": "postanalysis_simulations",
                   "experiments": "postanalysis_experiments", "runs": "postanalysis_runs",
                   "results": "postanalysis_results", "review_plans": "postanalysis_review_plans"}
    PREFIXES = {"datasets": "ds", "simulations": "sim", "experiments": "exp",
                "runs": "run", "results": "result", "review_plans": "plan"}

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
                    options.update(username=username,
                                   password=os.environ.get("CROWDSIM_MONGO_PASSWORD", ""),
                                   authSource=os.environ.get("CROWDSIM_MONGO_AUTH_SOURCE", "admin"))
                self.client = MongoClient(os.environ.get("CROWDSIM_MONGO_HOST", "127.0.0.1"),
                                          int(os.environ.get("CROWDSIM_MONGO_PORT", "27018")), **options)
            database = self.client[os.environ.get("CROWDSIM_MONGO_DATABASE", "crowdsim")]
        self.db = database
        self.files = GridFSBucket(database, bucket_name="postanalysis_files")
        self._index_lock = RLock()
        self._indexes_ready = False

    def close(self):
        if self.client is not None:
            self.client.close()

    def _ensure_indexes(self):
        if self._indexes_ready:
            return
        with self._index_lock:
            if self._indexes_ready:
                return
            for kind in self.KINDS:
                self.db[self.COLLECTIONS[kind]].create_index([("ownerId", 1), ("workspaceId", 1), ("createdAt", -1)])
            self.db[self.COLLECTIONS["review_plans"]].create_index(
                [("ownerId", 1), ("workspaceId", 1), ("sourceDraftId", 1)], unique=True,
                partialFilterExpression={"sourceDraftId": {"$type": "string"}},
            )
            self.db["postanalysis_events"].create_index([("kind", 1), ("itemId", 1), ("sequence", 1)], unique=True)
            self.db["postanalysis_events"].create_index([("ownerId", 1), ("workspaceId", 1), ("kind", 1), ("itemId", 1)])
            self.db["postanalysis_idempotency"].create_index("expiresAt", expireAfterSeconds=0)
            self._indexes_ready = True

    def _collection(self, kind: str):
        if kind not in self.KINDS:
            raise ValueError(f"未知资源类型: {kind}")
        self._ensure_indexes()
        return self.db[self.COLLECTIONS[kind]]

    def create(self, kind: str, user: str, workspace: str, payload: dict) -> dict:
        item_id = f"{self.PREFIXES[kind]}_{uuid4().hex}"
        timestamp = now()
        document = {"id": item_id, "ownerId": user, "workspaceId": workspace,
                    "createdAt": timestamp, "updatedAt": timestamp, **deepcopy(payload)}
        self._collection(kind).insert_one(document)
        return deepcopy(document)

    def create_dataset_with_file(self, user: str, workspace: str, payload: dict,
                                 filename: str, content_type: str, content: bytes) -> dict:
        """Store source bytes in GridFS, then link the parsed dataset to that object."""
        self._ensure_indexes()
        upload_id = self.files.upload_from_stream(
            filename,
            content,
            metadata={"ownerId": user, "workspaceId": workspace,
                      "contentType": content_type, "kind": "postanalysis_dataset_source"},
        )
        try:
            dataset = self.create("datasets", user, workspace, {
                **payload,
                "originalFile": {"fileId": str(upload_id), "name": filename,
                                 "contentType": content_type, "sizeBytes": len(content)},
            })
        except Exception:
            self.files.delete(upload_id)
            raise
        return dataset

    def save_review_plan(self, user: str, workspace: str, draft_id: str, payload: dict) -> dict:
        self._ensure_indexes()
        timestamp = now()
        item_id = f"{self.PREFIXES['review_plans']}_{uuid4().hex}"
        document = self.db[self.COLLECTIONS["review_plans"]].find_one_and_update(
            {"ownerId": user, "workspaceId": workspace, "sourceDraftId": draft_id},
            {"$set": {**deepcopy(payload), "updatedAt": timestamp},
             "$setOnInsert": {"id": item_id, "ownerId": user, "workspaceId": workspace,
                               "sourceDraftId": draft_id, "createdAt": timestamp}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
        document.pop("_id", None)
        return document

    def get(self, kind: str, item_id: str, user: str, workspace: str) -> dict:
        document = self._collection(kind).find_one({"id": item_id})
        if document is None:
            raise ApiError("NOT_FOUND", "资源不存在", 404)
        if document.get("ownerId") != user or document.get("workspaceId") != workspace:
            raise ApiError("RESOURCE_FORBIDDEN", "当前工作区无权访问此资源", 403)
        document.pop("_id", None)
        return document

    def update(self, kind: str, item_id: str, user: str, workspace: str, changes: dict) -> dict:
        timestamp = now()
        updated = self._collection(kind).find_one_and_update(
            {"id": item_id, "ownerId": user, "workspaceId": workspace},
            {"$set": {**deepcopy(changes), "updatedAt": timestamp}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            self.get(kind, item_id, user, workspace)
            raise ApiError("NOT_FOUND", "资源不存在", 404)
        updated.pop("_id", None)
        return updated

    def list(self, kind: str, user: str, workspace: str, *, limit: int = 50) -> list[dict]:
        items = self._collection(kind).find({"ownerId": user, "workspaceId": workspace}) \
            .sort([("createdAt", -1), ("id", 1)]).limit(limit)
        result = []
        for item in items:
            item.pop("_id", None)
            if kind == "datasets":
                item.pop("observations", None)
            result.append(item)
        return result

    def event(self, kind: str, item_id: str, event_type: str, detail: dict | None = None,
              user: str = "", workspace: str = "") -> None:
        self._ensure_indexes()
        counter_id = f"{kind}:{item_id}"
        counter = self.db["postanalysis_counters"].find_one_and_update(
            {"_id": counter_id}, {"$inc": {"sequence": 1}}, upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        self.db["postanalysis_events"].insert_one({
            "kind": kind, "itemId": item_id, "ownerId": user, "workspaceId": workspace,
            "sequence": counter["sequence"], "at": now(), "type": event_type, "detail": detail or {},
        })

    def events(self, kind: str, item_id: str, user: str, workspace: str,
               after: int, limit: int = 200) -> dict:
        self.get(kind, item_id, user, workspace)
        query = {"kind": kind, "itemId": item_id, "ownerId": user, "workspaceId": workspace,
                 "sequence": {"$gt": after}}
        events = list(self.db["postanalysis_events"].find(query, {"_id": 0})
                      .sort("sequence", 1).limit(limit))
        next_after = events[-1]["sequence"] if events else after
        return {"events": events, "nextAfter": next_after}

    def idempotent(self, user: str, workspace: str, method: str, path: str,
                   key: str, body: dict, operation) -> tuple[int, dict, bool]:
        self._ensure_indexes()
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                                 separators=(",", ":")).encode("utf-8")).hexdigest()
        lookup = hashlib.sha256(f"{user}\n{workspace}\n{method}\n{path}\n{key}".encode("utf-8")).hexdigest()
        collection = self.db["postanalysis_idempotency"]
        existing = collection.find_one({"_id": lookup})
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "相同幂等键对应了不同请求内容", 409)
            if existing.get("state") != "completed":
                raise ApiError("REQUEST_IN_PROGRESS", "同一请求仍在处理或需人工核对上次结果", 409)
            return existing["status"], existing["data"], True
        try:
            collection.insert_one({"_id": lookup, "fingerprint": fingerprint, "state": "in_progress",
                                   "createdAt": now(), "expiresAt": datetime.now(timezone.utc) + timedelta(days=30),
                                   "ownerId": user, "workspaceId": workspace})
        except DuplicateKeyError:
            raise ApiError("REQUEST_IN_PROGRESS", "同一请求仍在处理或需人工核对上次结果", 409)
        try:
            status, data = operation()
        except Exception:
            collection.delete_one({"_id": lookup, "state": "in_progress"})
            raise
        collection.update_one({"_id": lookup}, {"$set": {"state": "completed", "status": status,
                                                           "data": data, "completedAt": now()}})
        return status, data, False

    def reconcile_interrupted(self) -> None:
        self._ensure_indexes()
        self.db[self.COLLECTIONS["simulations"]].update_many(
            {"status": {"$in": ["queued", "running", "cancelling"]}},
            {"$set": {"status": "interrupted", "updatedAt": now()}})
        self.db[self.COLLECTIONS["experiments"]].update_many(
            {"status": {"$in": ["queued", "running", "cancelling"]}},
            {"$set": {"status": "interrupted", "updatedAt": now()}})
