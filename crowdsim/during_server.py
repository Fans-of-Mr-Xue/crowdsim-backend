"""Local during-event simulator without constructing the post-analysis application."""
import asyncio

from aiohttp import web

from crowdsim.infrastructure.websocket_server import OverlayServer
from crowdsim_overlay_server import parse_args, build_runtime, build_requirement_runtime, resolve_service_scenario


class DuringOverlayServer(OverlayServer):
    def create_app(self):
        app = web.Application()
        app.router.add_get("/", self._aio_websocket_handler)
        app.router.add_get("/api/v1/performance/config", self._performance_config)
        app.router.add_post("/api/v1/requirements", self._requirement_http_handler)
        self._register_class2_routes(app)
        async def health(_):
            return web.json_response({"service": "crowdsim-during-simulator", "databaseRequired": False})
        app.router.add_get("/api/v1/during/health", health)
        return app

    async def start(self):
        runner = web.AppRunner(self.create_app())
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
            print(f"[CrowdSim] during-only simulator listening at {self.host}:{self.port}", flush=True)
            await asyncio.Future()
        finally:
            await runner.cleanup()
            await self._stop_runtime()


def main():
    args = parse_args()
    selection = resolve_service_scenario(args)
    runtime = build_runtime(args, selection)
    factory = (lambda record: build_requirement_runtime(args, record)) if selection.name != "custom" else None
    asyncio.run(DuringOverlayServer(runtime, args.host, args.port, runtime_factory=factory).start())


if __name__ == "__main__":
    main()
