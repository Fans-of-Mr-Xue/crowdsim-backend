"""Standalone collection HTTP + workers; safe to restart while SUMO is running."""
import asyncio
from aiohttp import web
from pymongo.errors import PyMongoError
from ..api.news import register_news_routes
from .worker import run_worker


def create_crawler_app(database, *, concurrency=4):
    app = web.Application(client_max_size=256 * 1024)
    repository = register_news_routes(app, database.db)

    async def lifecycle(app):
        await asyncio.to_thread(repository.ensure_indexes)
        worker = asyncio.create_task(run_worker(repository, concurrency=concurrency))
        try:
            yield
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def status(request):
        try:
            result = await asyncio.to_thread(database.status)
            return web.json_response({"data": {**result, "service": "crowdsim-news-crawler"}})
        except PyMongoError:
            return web.json_response({"message": "数据库暂不可用"}, status=503)

    for prefix in ("/api/crowdSim", "/crowdSim"):
        app.router.add_get(prefix + "/news-collection/status", status)
    app.cleanup_ctx.append(lifecycle)
    return app
