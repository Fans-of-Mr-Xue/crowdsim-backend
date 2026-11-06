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
from postanalysis_api.repositories.trajectory_codec import encode as encode_trajectory, decode as decode_trajectory


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
        document.pop("_id", None)
        return deepcopy(document)

    def begin_archive(self, run_id, experiment_type, user, workspace, configuration):
        """Mongo has no child tables: a catalog points to three per-run collections."""
        allowed = {"simulation", "experiment", "observation", "intervention", "mechanism", "baseline", "counterfactual"}
        if experiment_type not in allowed:
            raise ValueError("Unknown post experiment type")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_name = f"{stamp}_{experiment_type}_{uuid4().hex[:8]}"
        collections = {"detail": f"post_{run_name}", "trajectory": f"post_trajectory_{run_name}",
                       "result": f"post_result_{run_name}"}
        catalog = self.db["post"]
        catalog.create_index("runId", unique=True)
        catalog.create_index([("ownerId", 1), ("workspaceId", 1), ("createdAt", -1)])
        entry = {"runId": run_id, "runName": run_name, "experimentType": experiment_type,
                 "ownerId": user, "workspaceId": workspace, "createdAt": now(),
                 "status": "running", "collections": collections}
        catalog.insert_one(deepcopy(entry))
        for name in collections.values():
            self.db[name].create_index([("runId", 1), ("sequence", 1)])
        self.db[collections["detail"]].insert_one({"runId": run_id, "sequence": 0,
                                                  "configuration": deepcopy(configuration), "createdAt": now()})
        return entry

    def archive_entry(self, run_id, user, workspace):
        item = self.db["post"].find_one({"runId": run_id, "ownerId": user, "workspaceId": workspace}, {"_id": 0})
        if item is None:
            raise ApiError("NOT_FOUND", "运行存档不存在或不属于当前工作区", 404)
        return item

    def append_trajectory(self, archive, sequence, frames):
        self.db[archive["collections"]["trajectory"]].insert_one(
            {"runId": archive["runId"], "sequence": sequence, **encode_trajectory(frames)})

    def archive_execution_inputs(self, archive, manifest_bytes, route_bytes, command):
        stored = []
        try:
            for name, content in (("input.json", manifest_bytes), ("demand.rou.xml", route_bytes)):
                identifier = self.files.upload_from_stream(name, content, metadata={
                    "ownerId": archive["ownerId"], "workspaceId": archive["workspaceId"],
                    "runId": archive["runId"], "kind": "post_sumo_execution_input"})
                stored.append(identifier)
            self.db[archive["collections"]["detail"]].insert_one({
                "runId": archive["runId"], "sequence": 1, "createdAt": now(),
                "compiledInput": {"manifestFileId": str(stored[0]), "routeFileId": str(stored[1]),
                                  "routeSha256": hashlib.sha256(route_bytes).hexdigest(),
                                  "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(), "command": command}})
        except Exception:
            for identifier in stored:
                self.files.delete(identifier)
            raise

    def finish_archive(self, archive, status, result):
        self.db[archive["collections"]["result"]].replace_one(
            {"runId": archive["runId"]}, {"runId": archive["runId"], "sequence": 0,
                                           "status": status, "result": deepcopy(result), "createdAt": now()}, upsert=True)
        self.db["post"].update_one({"runId": archive["runId"]},
                                 {"$set": {"status": status, "updatedAt": now()}})

    def trajectory(self, run_id, user, workspace, after=-1, limit=10):
        archive = self.archive_entry(run_id, user, workspace)
        items = list(self.db[archive["collections"]["trajectory"]].find(
            {"runId": run_id, "sequence": {"$gt": after}}, {"_id": 0}).sort("sequence", 1).limit(limit))
        items = [decode_trajectory(item) for item in items]
        return {"runId": run_id, "items": items, "nextAfter": items[-1]["sequence"] if items else after}

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

    def transition_task(self, kind, item_id, user, workspace, expected_status, changes):
        document = self._collection(kind).find_one_and_update(
            {"id": item_id, "ownerId": user, "workspaceId": workspace, "status": expected_status},
            {"$set": {**deepcopy(changes), "updatedAt": now()}}, return_document=ReturnDocument.AFTER)
        if document is None:
            self.get(kind, item_id, user, workspace)
            raise ApiError("INVALID_TASK_STATE", "任务状态已变化，请刷新后重试", 409)
        document.pop("_id", None)
        return document

    def runs_for_experiment(self, item_id, user, workspace):
        return list(self._collection("runs").find(
            {"experimentId": item_id, "ownerId": user, "workspaceId": workspace},
            {"_id": 0, "timeline": 0, "controlEvents": 0, "calibration.populationResiduals": 0})
            .sort([("createdAt", 1), ("id", 1)]).limit(650))

    def list(self, kind: str, user: str, workspace: str, *, limit: int = 50) -> list[dict]:
        items = self._collection(kind).find({"ownerId": user, "workspaceId": workspace}) \
            .sort([("createdAt", -1), ("id", 1)]).limit(limit)
        result = []
        for item in items:
            item.pop("_id", None)
            if kind == "datasets":
                item.pop("observations", None)
            if kind == "runs":
                item.pop("timeline", None)
                item.pop("controlEvents", None)
                item.get("calibration", {}).pop("populationResiduals", None)
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
        self.db[self.COLLECTIONS["runs"]].update_many(
            {"status": {"$in": ["queued", "running"]}},
            {"$set": {"status": "interrupted", "updatedAt": now()}})
        self.db["post"].update_many(
            {"status": {"$in": ["queued", "running"]}},
            {"$set": {"status": "interrupted", "updatedAt": now()}})
