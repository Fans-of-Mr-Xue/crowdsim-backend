"""Shared news storage, with atomic leases for independent crawler processes."""
from datetime import datetime, timezone, timedelta
import hashlib
import time
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from ..schemas.news import normalize, validate_task


def now():
    return datetime.now(timezone.utc)


class NewsRepository:
    def __init__(self, database):
        self.tasks = database["news_collection_tasks"]
        self.articles = database["news_articles"]
        self.workers = database["news_crawler_workers"]
        self.control = database["news_collection_control"]
        self.environments = database["environment_profiles"]
        self._indexes_ready = False

    def ensure_indexes(self):
        if self._indexes_ready:
            return
        self.consolidate_tasks()
        self.tasks.create_index("eventId", unique=True, partialFilterExpression={"canonical": True}, name="one_task_per_event")
        self.tasks.create_index([("status", 1), ("leaseUntil", 1), ("createdAt", 1)])
        self.tasks.create_index([("eventId", 1), ("createdAt", -1)])
        for field in ("urlHash", "titleHash", "contentHash"):
            self.articles.create_index([("eventId", 1), (field, 1)], unique=True)
        self.articles.create_index([("eventId", 1), ("publishedAt", -1)])
        self.workers.create_index("expiresAt", expireAfterSeconds=0)
        self._indexes_ready = True

    @staticmethod
    def public(doc):
        if doc is None:
            return None
        return {"id": str(doc["_id"]), **{k: (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat() if isinstance(v, datetime) else v
                for k, v in doc.items() if k not in {"_id", "leaseOwner", "leaseUntil", "content", "urlHash", "titleHash", "contentHash"}}}

    def article_query(self, task):
        return {"eventId": task["eventId"], "publishedAt": {"$gte": task["timeRange"][0], "$lte": task["timeRange"][1]}}

    def count(self, task):
        return min(task["maxRecords"], self.articles.count_documents(self.article_query(task)))

    @staticmethod
    def merge_config(existing, incoming):
        return {**{key: incoming[key] for key in ("eventName", "regionId", "location", "regionKeyword", "configSource")},
                "timeRange": [min(existing["timeRange"][0], incoming["timeRange"][0]),
                              max(existing["timeRange"][1], incoming["timeRange"][1])],
                "keywords": list(dict.fromkeys([*incoming["keywords"], *existing["keywords"]]))[:20],
                "seedUrls": list(dict.fromkeys([*incoming.get("seedUrls", []), *existing.get("seedUrls", [])]))[:50]}

    def consolidate_tasks(self):
        if self.tasks.find_one({"canonical": {"$exists": False}}, {"_id": 1}) is None:
            return
        # Several standalone workers may start together during this one-off upgrade.
        owner = uuid4().hex
        deadline = time.monotonic() + 30
        while True:
            try:
                self.control.find_one_and_update({"_id": "one-task-per-event", "$or": [
                    {"leaseUntil": {"$exists": False}}, {"leaseUntil": {"$lt": now()}}]},
                    {"$set": {"owner": owner, "leaseUntil": now() + timedelta(seconds=90)}}, upsert=True)
                break
            except DuplicateKeyError:
                if time.monotonic() >= deadline:
                    raise PyMongoError("另一个采集进程正在升级任务，请稍后重试") from None
                time.sleep(.1)
        try:
            self._consolidate_legacy_tasks(owner)
        finally:
            self.control.update_one({"_id": "one-task-per-event", "owner": owner},
                {"$unset": {"owner": "", "leaseUntil": ""}, "$set": {"updatedAt": now()}})

    def _consolidate_legacy_tasks(self, owner):
        # Archive legacy duplicates, preserving IDs as aliases and article references.
        events = {task["eventId"] for task in self.tasks.find({"canonical": {"$exists": False}})}
        for event_id in events:
            renewed = self.control.update_one({"_id": "one-task-per-event", "owner": owner},
                {"$set": {"leaseUntil": now() + timedelta(seconds=90)}})
            if not renewed.matched_count:
                raise PyMongoError("任务升级租约已失效，请稍后重试")
            tasks = list(self.tasks.find({"eventId": event_id, "canonical": {"$ne": False}}).sort([("createdAt", 1), ("_id", 1)]))
            if not tasks:
                continue
            canonical = tasks[0]
            if len(tasks) == 1:
                self.tasks.update_one({"_id": canonical["_id"]}, {"$set": {
                    "canonical": True, "revision": canonical.get("revision", 0),
                    "lastBatchSize": canonical.get("lastBatchSize", canonical["maxRecords"])}})
                continue
            config = canonical
            for task in tasks[1:]:
                config = {**config, **self.merge_config(config, task)}
            goal = sum(task["maxRecords"] for task in tasks)
            count = self.articles.count_documents(self.article_query({**config, "eventId": event_id}))
            latest = tasks[-1]
            active = any(task["status"] in {"queued", "running"} for task in tasks)
            status = "queued" if active else "cancelled" if latest["status"] == "cancelled" else "completed" if count >= goal else "partial" if count else "failed"
            for duplicate in tasks[1:]:
                self.tasks.update_one({"_id": duplicate["_id"]}, {"$set": {
                    "canonical": False, "mergedInto": canonical["_id"], "status": "merged", "updatedAt": now()},
                    "$unset": {"leaseOwner": "", "leaseUntil": ""}, "$inc": {"revision": 1}})
            fields = {key: config[key] for key in ("eventName", "regionId", "location", "regionKeyword", "timeRange", "keywords", "seedUrls", "configSource")}
            fields.update(canonical=True, revision=canonical.get("revision", 0) + 1, maxRecords=max(goal, count),
                          lastBatchSize=latest.get("lastBatchSize", latest["maxRecords"]), recordCount=count,
                          timeWindow=" 至 ".join(config["timeRange"]), status=status,
                          updatedAt=now(), message=f"同事件任务已合并，已入库 {count} 条新闻")
            for key in ("processedCount", "rejectedCount", "duplicateCount", "errorCount", "attempts"):
                fields[key] = sum(task.get(key, 0) for task in tasks)
            self.tasks.update_one({"_id": canonical["_id"]}, {"$set": fields, "$unset": {"leaseOwner": "", "leaseUntil": "", "finishedAt": ""}})

    def resolve(self, identifier, *, include_deleted=False):
        self.ensure_indexes()
        task = self.tasks.find_one({"_id": identifier})
        seen = set()
        while task and task.get("mergedInto") and task["_id"] not in seen:
            seen.add(task["_id"])
            task = self.tasks.find_one({"_id": task["mergedInto"]})
        return task if task and (include_deleted or "deletedAt" not in task) else None

    def create(self, payload):
        config = validate_task(payload)
        region = self.environments.find_one({"_id": config["regionId"], "is_deleted": {"$ne": True}})
        if region is None:
            raise ValueError("采集地区不存在，请先在环境库新建并保存地区")
        config["location"] = region["locationLabel"]
        self.ensure_indexes()
        identity = normalize(config["eventName"]) + "|" + config["regionId"]
        event_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        batch_size = config["maxRecords"]
        while True:
            existing = self.tasks.find_one({"eventId": event_id, "canonical": True})
            if existing is None:
                saved = self.articles.count_documents(self.article_query({**config, "eventId": event_id}))
                task = {**config, "_id": uuid4().hex, "eventId": event_id, "canonical": True, "revision": 0,
                        "lastBatchSize": batch_size, "maxRecords": saved + batch_size,
                        "status": "queued", "createdAt": now(), "updatedAt": now(), "attempts": 0,
                        "processedCount": 0, "rejectedCount": 0, "duplicateCount": 0, "errorCount": 0,
                        "recordCount": saved, "message": "等待独立爬虫进程领取任务", "sourceErrors": []}
                try:
                    self.tasks.insert_one(task)
                    return {**self.public(task), "operation": "created", "requestedRecords": batch_size}
                except DuplicateKeyError:
                    continue  # Another caller created this event; append atomically below.
            merged = self.merge_config(existing, config)
            # The editable tag set is authoritative, including user removals.
            merged['keywords'] = config['keywords']
            saved = self.articles.count_documents(self.article_query({**merged, "eventId": event_id}))
            active = "deletedAt" not in existing and existing["status"] in {"queued", "running"}
            target = max(saved, existing["maxRecords"]) if active else saved
            fields = {**merged, "timeWindow": " 至 ".join(merged["timeRange"]), "maxRecords": target + batch_size,
                      "lastBatchSize": batch_size, "recordCount": saved, "updatedAt": now(), "sourceErrors": [],
                      "status": "running" if active and existing["status"] == "running" else "queued",
                      "message": f"已追加 {batch_size} 条采集需求，累计目标 {target + batch_size} 条"}
            unset = {"deletedAt": "", "finishedAt": ""}
            if fields["status"] != "running":
                unset.update(leaseOwner="", leaseUntil="")
            task = self.tasks.find_one_and_update({"_id": existing["_id"], "revision": existing["revision"]},
                {"$set": fields, "$unset": unset, "$inc": {"revision": 1}}, return_document=ReturnDocument.AFTER)
            if task is not None:
                return {**self.public(task), "operation": "appended", "requestedRecords": batch_size}

    def get(self, identifier):
        task = self.resolve(identifier)
        if task:
            task["recordCount"] = self.count(task)
        return self.public(task)

    def list(self, page=1, page_size=100):
        self.ensure_indexes()
        visible = {"canonical": True, "deletedAt": {"$exists": False}}
        items = []
        for task in self.tasks.find(visible).sort("createdAt", -1).skip((page - 1) * page_size).limit(page_size):
            task["recordCount"] = self.count(task)
            items.append(self.public(task))
        return {"items": items, "total": self.tasks.count_documents(visible), "page": page, "page_size": page_size,
                "pendingCount": self.tasks.count_documents({**visible, "status": {"$in": ["queued", "running"]}}),
                "workerCount": self.workers.count_documents({"expiresAt": {"$gt": now()}})}

    def news(self, identifier, page=1, page_size=50):
        task = self.resolve(identifier)
        if not task:
            raise KeyError(identifier)
        total = self.count(task)
        offset = (page - 1) * page_size
        size = max(0, min(page_size, total - offset))
        items = [] if size == 0 else [self.public(doc) for doc in self.articles.find(
            self.article_query(task), {"content": 0}).sort([("publishedAt", -1), ("_id", 1)]).skip(offset).limit(size)]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def cancel(self, identifier):
        task = self.resolve(identifier)
        if task is None:
            raise KeyError(identifier)
        identifier = task["_id"]
        result = self.tasks.find_one_and_update({"_id": identifier, "status": {"$in": ["queued", "running"]}},
            {"$set": {"status": "cancelled", "updatedAt": now(), "message": "任务已停止"},
             "$unset": {"leaseOwner": "", "leaseUntil": ""}, "$inc": {"revision": 1}}, return_document=ReturnDocument.AFTER)
        if result is None:
            if not self.tasks.find_one({"_id": identifier}):
                raise KeyError(identifier)
            raise ValueError("只有排队或运行中的任务可以停止")
        return self.public(result)

    def delete(self, identifier):
        task = self.resolve(identifier, include_deleted=True)
        if task is None:
            raise KeyError(identifier)
        self.tasks.update_one({"_id": task["_id"]}, {"$set": {
            "status": "deleted", "deletedAt": now(), "updatedAt": now(), "message": "任务已删除，已入库新闻保留"},
            "$unset": {"leaseOwner": "", "leaseUntil": ""}, "$inc": {"revision": 1}})
        return {"id": task["_id"], "deleted": True, "newsRetained": True}

    def refresh_locations(self, identifier=None):
        """Backfill only geographic metadata; preserve article content and task state."""
        from ..crawlers.locations import article_locations
        query = {}
        if identifier:
            task = self.tasks.find_one({"_id": identifier})
            if task is None:
                raise KeyError(identifier)
            query = self.article_query(task)
        updated = 0
        for article in self.articles.find(query):
            task = self.tasks.find_one({"_id": article["taskId"]})
            if task is None:
                continue
            fields = article_locations(article["title"], article["content"], task["regionId"])
            if any(article.get(key) != value for key, value in fields.items()):
                updated += self.articles.update_one({"_id": article["_id"]}, {"$set": fields}).modified_count
        return updated

    def claim(self, owner, identifier=None):
        query = {"canonical": True, "deletedAt": {"$exists": False}, "$or": [{"status": "queued"}, {"status": "running", "leaseUntil": {"$lt": now()}}]}
        if identifier:
            task = self.resolve(identifier)
            if task is None:
                return None
            query["_id"] = task["_id"]
        return self.tasks.find_one_and_update(query,
            {"$set": {"status": "running", "leaseOwner": owner, "leaseUntil": now() + timedelta(seconds=90),
                      "updatedAt": now(), "message": "正在发现和校验公开新闻"}, "$inc": {"attempts": 1, "revision": 1}},
            sort=[("createdAt", 1)], return_document=ReturnDocument.AFTER)

    def owns(self, identifier, owner):
        return {"_id": identifier, "status": "running", "leaseOwner": owner, "leaseUntil": {"$gt": now()}}

    def heartbeat(self, identifier, owner):
        return bool(self.tasks.update_one(self.owns(identifier, owner),
            {"$set": {"leaseUntil": now() + timedelta(seconds=90), "updatedAt": now()}}).matched_count)

    def worker_heartbeat(self, owner, concurrency):
        self.workers.update_one({"_id": owner}, {"$set": {"concurrency": concurrency, "expiresAt": now() + timedelta(seconds=45)}}, upsert=True)

    def progress(self, task, owner, **values):
        values.update(updatedAt=now(), recordCount=self.count(task))
        return bool(self.tasks.update_one(self.owns(task["_id"], owner), {"$set": values}).matched_count)

    def finish(self, task, owner, status, message):
        owned = self.owns(task["_id"], owner)
        completed = self.tasks.update_one({**owned, "revision": task.get("revision", 0)},
            {"$set": {"status": status, "message": message, "recordCount": self.count(task), "updatedAt": now(), "finishedAt": now()},
             "$unset": {"leaseOwner": "", "leaseUntil": ""}, "$inc": {"revision": 1}}).matched_count
        if not completed:
            # An append during a running crawl must not be marked done using the old goal.
            self.tasks.update_one({**owned, "revision": {"$ne": task.get("revision", 0)}},
                {"$set": {"status": "queued", "updatedAt": now(), "message": "继续处理追加采集需求"},
                 "$unset": {"leaseOwner": "", "leaseUntil": ""}, "$inc": {"revision": 1}})
        return completed

    def save_article(self, task, owner, article):
        # A cancelled or reclaimed job must stop before writing another result.
        if not self.tasks.find_one(self.owns(task["_id"], owner), {"_id": 1}):
            return "stopped"
        doc = {**article, "_id": uuid4().hex, "eventId": task["eventId"], "taskId": task["_id"], "collectedAt": now()}
        for name, value in (("urlHash", article["url"]), ("titleHash", normalize(article["title"])),
                            ("contentHash", normalize(article["content"]))):
            doc[name] = hashlib.sha256(value.encode()).hexdigest()
        try:
            self.articles.insert_one(doc)
            return "saved"
        except DuplicateKeyError:
            return "duplicate"
