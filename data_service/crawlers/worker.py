"""Lease-backed concurrent queue consumer. No dependency on SUMO or the HTTP server."""
import asyncio
import logging
import aiohttp
from uuid import uuid4
from pymongo.errors import PyMongoError
from ..repositories.news import NewsRepository
from .news_sources import NewsSources, extract_article, seed_candidates

log = logging.getLogger(__name__)


async def collect(repository, sources, task, owner):
    counters = {key: task.get(key, 0) for key in ("processedCount", "rejectedCount", "duplicateCount", "errorCount")}
    lost = asyncio.Event()

    async def renew():
        while True:
            await asyncio.sleep(15)
            if not await asyncio.to_thread(repository.heartbeat, task["_id"], owner):
                lost.set()
                return

    heartbeat = asyncio.create_task(renew())
    try:
        if await asyncio.to_thread(repository.count, task) >= task["maxRecords"]:
            await asyncio.to_thread(repository.finish, task, owner, "completed", "已有新闻已满足目标条数，复用持久化结果")
            return
        async def batches():
            yield seed_candidates(task), []
            if not lost.is_set() and await asyncio.to_thread(repository.count, task) < task["maxRecords"]:
                yield await sources.discover(task)

        seen = set()
        async for candidates, source_errors in batches():
            candidates = [item for item in candidates if item["url"] not in seen]
            seen.update(item["url"] for item in candidates)
            await asyncio.to_thread(repository.progress, task, owner, foundCount=len(seen), sourceErrors=source_errors)
            for candidate in candidates[:max(100, task["maxRecords"] * 5)]:
                if lost.is_set() or not await asyncio.to_thread(repository.heartbeat, task["_id"], owner):
                    return
                if await asyncio.to_thread(repository.count, task) >= task["maxRecords"]:
                    break
                counters["processedCount"] += 1
                try:
                    html, url = await sources.fetch(candidate["url"])
                    try:
                        article = await asyncio.to_thread(extract_article, html, url, task, candidate)
                    except ValueError as error:
                        counters["rejectedCount"] += 1
                        await asyncio.to_thread(repository.progress, task, owner, **counters, message=str(error))
                        continue
                    saved = await asyncio.to_thread(repository.save_article, task, owner, article)
                    if saved == "stopped":
                        return
                    if saved == "duplicate":
                        counters["duplicateCount"] += 1
                    message = "正在抓取、筛选并保存新闻正文"
                except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as error:
                    counters["errorCount"] += 1
                    message = f"部分原文无法访问：{type(error).__name__}"
                await asyncio.to_thread(repository.progress, task, owner, **counters, message=message)
        count = await asyncio.to_thread(repository.count, task)
        status = "completed" if count >= task["maxRecords"] else "partial" if count else "failed"
        message = f"已保存 {count}/{task['maxRecords']} 条不同新闻"
        if status != "completed":
            message += "；可访问来源中符合时间窗与关键词的新闻不足，可补充原文链接后追加采集"
        await asyncio.to_thread(repository.finish, task, owner, status, message)
    except asyncio.CancelledError:
        # Leave lease intact; another process may reclaim after expiry.
        raise
    except Exception:
        log.exception("采集任务失败 %s", task["_id"])
        try:
            await asyncio.to_thread(repository.finish, task, owner, "failed", "采集执行失败，请检查独立爬虫进程日志")
        except PyMongoError:
            log.error("数据库暂不可用，任务将在租约到期后恢复")
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def run_worker(repository, *, concurrency=4, once=False, identifier=None, sources_factory=NewsSources):
    await asyncio.to_thread(repository.ensure_indexes)
    worker_id = uuid4().hex

    async def presence():
        while True:
            try:
                await asyncio.to_thread(repository.worker_heartbeat, worker_id, concurrency)
            except PyMongoError:
                log.error("数据库暂不可用，稍后重试")
            await asyncio.sleep(15)

    presence_task = asyncio.create_task(presence())
    async with sources_factory() as sources:
        async def consume(slot):
            owner = f"{worker_id}-{slot}"
            while True:
                try:
                    task = await asyncio.to_thread(repository.claim, owner, identifier)
                    if task:
                        log.info("领取任务 %s：%s", task["_id"], task["eventName"])
                        await collect(repository, sources, task, owner)
                        continue
                    if once or identifier:
                        return
                except PyMongoError:
                    if once or identifier:
                        raise
                    log.error("数据库暂不可用，领取任务稍后重试")
                await asyncio.sleep(3)
        try:
            await asyncio.gather(*(consume(slot) for slot in range(concurrency)))
        finally:
            presence_task.cancel()
            await asyncio.gather(presence_task, return_exceptions=True)
            await asyncio.to_thread(repository.workers.delete_one, {"_id": worker_id})
