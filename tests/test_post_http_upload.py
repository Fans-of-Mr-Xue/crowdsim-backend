"""HTTP uploads must consume all network fragments before decoding JSON."""
import asyncio
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from postanalysis_api.main import handle_http


class PostHttpUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []

        def dispatch(request):
            self.received.append(request.body)
            return 201, {"accepted": True}, False

        self.dispatch_patch = patch("postanalysis_api.main._dispatch", side_effect=dispatch)
        self.dispatch_patch.start()
        self.addCleanup(self.dispatch_patch.stop)

        async def handler(request):
            return await handle_http(request, None)

        application = web.Application()
        application.router.add_post("/api/v1/post/datasets", handler)
        self.client = TestClient(TestServer(application))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def upload(self, raw, *, split=1, content_length=False, content_type="application/json"):
        async def fragments():
            yield raw[:split]
            # Force the server to receive an incomplete prefix first.
            await asyncio.sleep(0.02)
            for offset in range(split, len(raw), 4096):
                yield raw[offset:offset + 4096]
                await asyncio.sleep(0)

        headers = {"Content-Type": content_type}
        if content_length:
            headers["Content-Length"] = str(len(raw))
        async with self.client.post("/api/v1/post/datasets", data=fragments(), headers=headers) as response:
            return response.status, await response.json()

    async def test_complete_memorial_file_with_raw_attachment_survives_fragmented_utf8(self):
        source = Path(__file__).resolve().parents[1] / "docs/examples/shanghai_memorial_post_dataset.json"
        original = source.read_bytes()
        payload = json.loads(original.decode("utf-8"))
        payload["rawFile"] = {"name": source.name, "contentType": "application/json",
                              "data": "data:application/json;base64," + base64.b64encode(original).decode("ascii")}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        split = next(index + 1 for index, byte in enumerate(raw) if byte >= 0xE0)
        for content_length in (True, False):
            with self.subTest(content_length=content_length):
                status, result = await self.upload(raw, split=split, content_length=content_length)
                self.assertEqual(201, status, result)
                self.assertTrue(result["success"])
                self.assertEqual(payload, self.received[-1])

    async def test_invalid_utf8_malformed_json_and_nonobject_are_rejected(self):
        for raw in (b'{"name":"\xff"}', b'{"name":', b'[]'):
            with self.subTest(raw=raw):
                status, result = await self.upload(raw)
                self.assertEqual(400, status)
                self.assertEqual("INVALID_JSON", result["error"]["code"])
        self.assertEqual([], self.received)

    async def test_size_limit_applies_to_fixed_length_and_chunked_bodies(self):
        raw = json.dumps({"name": "a" * 300}).encode("utf-8")
        with patch("postanalysis_api.main.MAX_REQUEST_BYTES", 256):
            for content_length in (True, False):
                with self.subTest(content_length=content_length):
                    status, result = await self.upload(raw, split=150, content_length=content_length)
                    self.assertEqual(413, status)
                    self.assertEqual("PAYLOAD_TOO_LARGE", result["error"]["code"])
        self.assertEqual([], self.received)

    async def test_exact_size_limit_is_accepted_and_media_type_is_checked(self):
        raw = b'{"name":"valid"}'
        with patch("postanalysis_api.main.MAX_REQUEST_BYTES", len(raw)):
            status, _ = await self.upload(raw)
            self.assertEqual(201, status)
            status, result = await self.upload(raw, content_type="text/plain")
            self.assertEqual(415, status)
            self.assertEqual("UNSUPPORTED_MEDIA_TYPE", result["error"]["code"])


if __name__ == "__main__":
    unittest.main()
