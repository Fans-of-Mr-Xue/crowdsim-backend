"""Shared MongoDB dataset repository for HTTP clients and future importers."""
from __future__ import annotations

from datetime import datetime, timezone
import os
import math
import re
from uuid import uuid4

from .config import load_database_env
from pymongo import MongoClient, ReturnDocument


class DatasetConflictError(Exception):
    pass


def validate_dataset(payload):
    if not isinstance(payload, dict):
        raise ValueError("数据集必须是 JSON 对象")
    unknown = set(payload) - {"name", "source", "description", "records"}
    if unknown:
        raise ValueError("不支持的字段：" + ", ".join(sorted(unknown)))
    result = {}
    for key, limit in (("name", 200), ("source", 200), ("description", 4000)):
        value = payload.get(key, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"{key} 必须是长度不超过 {limit} 的文本")
        result[key] = value.strip()
    if not result["name"]:
        raise ValueError("请输入数据集名称")
    records = payload.get("records", [])
    if not isinstance(records, list) or len(records) > 10000 or any(not isinstance(row, dict) for row in records):
        raise ValueError("records 必须是最多 10000 条 JSON 对象组成的数组")
    def check_keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or key.startswith("$") or "." in key or "\x00" in key:
                    raise ValueError("数据字段不能包含点、空字符或以 $ 开头")
                check_keys(child)
        elif isinstance(value, list):
            for child in value:
                check_keys(child)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("数值必须是有限数")
        elif type(value) is int and not -(2 ** 63) <= value < 2 ** 63:
            raise ValueError("整数必须在 64 位有符号范围内")
    check_keys(records)
    result["records"] = records
    result["record_count"] = len(records)
    return result


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
