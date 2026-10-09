"""Independent collection service, with optional additional queue-only consumers."""
import argparse
import asyncio
import logging
from ..repositories.datasets import DatasetRepository
from ..repositories.news import NewsRepository
from .worker import run_worker


def main():
    parser = argparse.ArgumentParser(description="独立新闻爬虫队列，不启动 SUMO 或模型服务")
    parser.add_argument("--concurrency", type=int, default=4, help="并行任务数，1—16")
    parser.add_argument("--once", action="store_true", help="处理当前队列后退出")
    parser.add_argument("--task", help="只领取指定任务，结束后退出")
    parser.add_argument("--worker-only", action="store_true", help="只消费任务，不启动采集 HTTP 服务；允许多进程并行")
    parser.add_argument("--enrich-locations", action="store_true", help="为已保存新闻补全原文提及地点后退出；可结合 --task")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 16:
        parser.error("并行任务数必须为 1—16")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    database = DatasetRepository()
    try:
        if args.enrich_locations:
            updated = NewsRepository(database.db).refresh_locations(args.task)
            print(f"已更新 {updated} 条新闻的地点信息")
        elif args.worker_only or args.once or args.task:
            asyncio.run(run_worker(NewsRepository(database.db), concurrency=args.concurrency, once=args.once, identifier=args.task))
        else:
            from aiohttp import web
            from .server import create_crawler_app
            web.run_app(create_crawler_app(database, concurrency=args.concurrency), host=args.host, port=args.port, access_log=None)
    except KeyboardInterrupt:
        pass
    finally:
        database.close()


if __name__ == "__main__":
    main()
