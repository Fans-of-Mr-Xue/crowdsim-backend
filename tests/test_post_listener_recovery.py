"""A duplicate listener must not interrupt the worker belonging to the live server."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web

from crowdsim.infrastructure.websocket_server import OverlayServer


class PostListenerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_bind_never_reconciles_and_still_closes_the_new_instance(self):
        listener = await asyncio.start_server(lambda _reader, writer: writer.close(), '127.0.0.1', 0)
        app = Mock()
        server = OverlayServer.__new__(OverlayServer)
        server.host, server.port = '127.0.0.1', listener.sockets[0].getsockname()[1]
        server.create_app = lambda: web.Application()
        server._stop_runtime = AsyncMock()
        try:
            with patch('crowdsim.infrastructure.websocket_server.PostAnalysisApp', return_value=app) as constructor:
                with self.assertRaises(OSError):
                    await server.start()
            self.assertFalse(constructor.call_args.kwargs['reconcile'])
            app.recover_interrupted.assert_not_called()
            app.close.assert_called_once()
            server._stop_runtime.assert_awaited_once()
        finally:
            listener.close()
            await listener.wait_closed()

    async def test_successful_listener_recovers_once_and_keeps_exit_cleanup(self):
        recovered = asyncio.Event()
        app = Mock()
        app.recover_interrupted.side_effect = recovered.set
        server = OverlayServer.__new__(OverlayServer)
        server.host, server.port = '127.0.0.1', 0
        server.create_app = lambda: web.Application()
        server._stop_runtime = AsyncMock()
        with patch('crowdsim.infrastructure.websocket_server.PostAnalysisApp', return_value=app):
            task = asyncio.create_task(server.start())
            await asyncio.wait_for(recovered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        app.recover_interrupted.assert_called_once()
        app.close.assert_called_once()
        server._stop_runtime.assert_awaited_once()
