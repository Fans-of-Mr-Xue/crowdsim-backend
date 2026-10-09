"""Emergency plan metadata and original PDFs stored in MongoDB/GridFS."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import suppress
import re
from uuid import uuid4

from gridfs import GridFSBucket
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from .datasets import DatasetConflictError


def now():
    return datetime.now(timezone.utc).isoformat()


class KnowledgeDocumentRepository:
    def __init__(self, database, profile):
        self.profile = profile
        self.collection = database[profile.collection]
        bucket_name = "emergency_plan_files" if profile.collection == "emergency_plans" else profile.collection + "_files"
        self.bucket = GridFSBucket(database, bucket_name=bucket_name)

    def initialize(self):
        # Mongo creates collections on first use; the indexes also create the metadata collection.
        self.collection.create_index([("updated_at", -1)])
        self.collection.create_index([("is_deleted", 1), ("parse_status", 1), ("lease_until", 1), ("created_at", 1)])

    def public(self, doc):
        if doc is None:
            return None
        hidden = {"_id", "file_id", "image_file_id", "run_id", "lease_until"}
        return {"id": doc["_id"], **{k: v for k, v in doc.items() if k not in hidden},
                "file_url": f"/api/crowdSim/{self.profile.endpoint}/{doc['_id']}/file",
                "region_image": {**doc["region_image"], "url": f"/api/crowdSim/{self.profile.endpoint}/{doc['_id']}/image"} if doc.get("region_image") else None}

    def create(self, source, filename, size, checksum, *, source_kind="pdf", source_url="", title=""):
        identifier = uuid4().hex
        metadata = self.profile.validate({"name": (title or filename.rsplit(".", 1)[0])[:200], "type": self.profile.label})
        mime = "application/pdf" if source_kind == "pdf" else "text/html"
        upload = self.bucket.open_upload_stream(filename, metadata={"document_id": identifier, "contentType": mime})
        try:
            upload.write(source)
            upload.close()
        except Exception:
            with suppress(PyMongoError):
                upload.abort()
            raise
        file_id = upload._id
        timestamp = now()
        doc = {"_id": identifier, **metadata,
               "file_id": file_id, "file_name": filename, "file_size": size, "sha256": checksum,
               "source_kind": source_kind, "source_url": source_url, "file_mime": mime,
               "parse_status": "queued", "parse_error": "", "review_status": "pending", "is_deleted": False,
               "parse_progress": {"completed": 0, "total": 0}, "revision": 1,
               "created_at": timestamp, "updated_at": timestamp}
        try:
            self.collection.insert_one(doc)
        except Exception:
            self.bucket.delete(file_id)
            raise
        return self.public(doc)

    def get(self, identifier):
        return self.public(self.collection.find_one({"_id": identifier, "is_deleted": {"$ne": True}}))

    def list(self, page=1, page_size=20, search="", topic=""):
        query = {"is_deleted": {"$ne": True}}
        if search:
            expression = {"$regex": re.escape(search), "$options": "i"}
            query["$or"] = [{key: expression} for key in ("name", "type", "summary", "tags")]
        if topic:
            query["topic"] = topic
        cursor = self.collection.aggregate([{ "$match": query },
            {"$addFields": {"_parse_order": {"$cond": [{"$eq": ["$parse_status", "parsed"]}, 0, 1]}}},
            {"$sort": {"_parse_order": 1, "updated_at": -1, "_id": 1}},
            {"$skip": (page - 1) * page_size}, {"$limit": page_size}, {"$unset": "_parse_order"}])
        items = [self.public(doc) for doc in cursor]
        active = {"is_deleted": {"$ne": True}}
        stats = {"total": self.collection.count_documents(active)}
        for state in ("queued", "parsing", "parsed", "failed"):
            stats[state] = self.collection.count_documents({**active, "parse_status": state})
        return {"items": items, "total": self.collection.count_documents(query), "page": page, "page_size": page_size, "stats": stats}

    def update(self, identifier, payload, revision):
        metadata = self.profile.validate(payload)
        result = self.collection.find_one_and_update(
            {"_id": identifier, "is_deleted": {"$ne": True}, "revision": revision, "parse_status": {"$nin": ["queued", "parsing"]}},
            {"$set": {**metadata, "review_status": "reviewed", "updated_at": now()}, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER)
        if result is None:
            self.conflict(identifier)
        return self.public(result)

    def conflict(self, identifier):
        if self.collection.find_one({"_id": identifier, "is_deleted": {"$ne": True}}, {"_id": 1}) is None:
            raise KeyError(identifier)
        raise DatasetConflictError("资料正在解析或已被其他用户修改，请刷新后重试")

    def retry(self, identifier, revision):
        doc = self.collection.find_one_and_update(
            {"_id": identifier, "is_deleted": {"$ne": True}, "revision": revision, "parse_status": {"$in": ["parsed", "failed"]}},
            {"$set": {"parse_status": "queued", "parse_error": "", "review_status": "pending",
                      "parse_progress": {"completed": 0, "total": 0}, "updated_at": now()}, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER)
        if doc is None:
            self.conflict(identifier)
        return self.public(doc)

    def delete(self, identifier, revision):
        doc = self.collection.find_one_and_update(
            {"_id": identifier, "revision": revision, "is_deleted": {"$ne": True}},
            {"$set": {"is_deleted": True, "updated_at": now()}, "$inc": {"revision": 1}})
        if doc is None:
            self.conflict(identifier)

    def open_file(self, identifier):
        doc = self.collection.find_one({"_id": identifier, "is_deleted": {"$ne": True}})
        if doc is None:
            raise KeyError(identifier)
        return self.bucket.open_download_stream(doc["file_id"]), doc["file_name"]

    def save_image(self, identifier, run_id, image):
        if image is None:
            return True
        upload = self.bucket.open_upload_stream("region-image.png", metadata={"document_id": identifier, "contentType": "image/png"})
        try:
            upload.write(image["data"])
            upload.close()
        except Exception:
            with suppress(PyMongoError):
                upload.abort()
            raise
        selector = {"_id": identifier, "is_deleted": {"$ne": True}, "run_id": run_id, "parse_status": "parsing"}
        details = {key: value for key, value in image.items() if key != "data"}
        try:
            previous = self.collection.find_one_and_update(selector, {"$set": {"image_file_id": upload._id, "region_image": details}})
        except Exception:
            self.bucket.delete(upload._id)
            raise
        if previous is None:
            self.bucket.delete(upload._id)
            return False
        if previous.get("image_file_id"):
            self.bucket.delete(previous["image_file_id"])
        return True

    def open_image(self, identifier):
        doc = self.collection.find_one({"_id": identifier, "is_deleted": {"$ne": True}})
        if doc is None or not doc.get("image_file_id"):
            raise KeyError(identifier)
        return self.bucket.open_download_stream(doc["image_file_id"])

    def copy_file(self, identifier, destination):
        stream, _ = self.open_file(identifier)
        with stream:
            while chunk := stream.read(256 * 1024):
                destination.write(chunk)
        destination.seek(0)

    def claim(self, run_id):
        return self.collection.find_one_and_update(
            {"is_deleted": {"$ne": True}, "$or": [{"parse_status": "queued"}, {"parse_status": "parsing", "lease_until": {"$lt": now()}}]},
            {"$set": {"parse_status": "parsing", "run_id": run_id, "lease_until": self.lease(), "parse_error": ""}},
            sort=[("created_at", 1)], return_document=ReturnDocument.AFTER)

    @staticmethod
    def lease():
        return (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()

    def heartbeat(self, identifier, run_id):
        return self.collection.update_one({"_id": identifier, "is_deleted": {"$ne": True}, "run_id": run_id, "parse_status": "parsing"},
                                          {"$set": {"lease_until": self.lease()}}).matched_count == 1

    def progress(self, identifier, run_id, completed, total):
        return self.collection.update_one({"_id": identifier, "is_deleted": {"$ne": True}, "run_id": run_id, "parse_status": "parsing"},
                                   {"$set": {"parse_progress": {"completed": completed, "total": total}}}).matched_count == 1

    def finish(self, identifier, run_id, *, metadata=None, error="", details=None):
        fields = {"parse_status": "failed" if error else "parsed", "parse_error": error,
                  "updated_at": now(), "review_status": "pending"}
        if metadata is not None:
            fields.update(self.profile.validate(metadata))
            fields["extraction"] = details
        self.collection.update_one({"_id": identifier, "is_deleted": {"$ne": True}, "run_id": run_id, "parse_status": "parsing"},
                                   {"$set": fields, "$unset": {"run_id": "", "lease_until": ""}, "$inc": {"revision": 1}})

    def release(self, identifier, run_id):
        self.collection.update_one({"_id": identifier, "is_deleted": {"$ne": True}, "run_id": run_id, "parse_status": "parsing"},
                                   {"$set": {"parse_status": "queued"}, "$unset": {"run_id": "", "lease_until": ""}})
