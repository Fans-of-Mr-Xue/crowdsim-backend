"""Independent data HTTP process; never starts SUMO or background crawlers."""
import argparse

from aiohttp import web

from .api import create_app


def main():
    parser = argparse.ArgumentParser(description="CrowdSim data service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    web.run_app(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
