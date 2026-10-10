"""Streaming PDF imports and shared emergency plan HTTP endpoints."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import tempfile
from urllib.parse import quote

from aiohttp import ClientConnectionError, web
from gridfs import NoFile
from pymongo.errors import PyMongoError

from ..repositories.knowledge_documents import KnowledgeDocumentRepository
from ..connectors.llm import JsonModelClient
from ..services.knowledge_documents import DocumentParser
from ..services.document_sources import file_io, validate_pdf
from ..connectors.web_sources import fetch_document
from ..repositories.datasets import DatasetConflictError


def register_document_routes(app, database, profile, *, model=None):
    repository = KnowledgeDocumentRepository(database, profile)
    model = model if model is not None else JsonModelClient()

    async def lifecycle(app):
        # Do not require a live database/model merely to open the HTTP port.
        async def work():
            while True:
                try:
                    await asyncio.to_thread(repository.initialize)
                    break
                except PyMongoError:
                    await asyncio.sleep(3)
            await DocumentParser(repository, model).run()

        key = "knowledge_model_" + str(id(model))
        if key not in app:
            await model.open()
            app[key] = 0
        app[key] += 1
        worker = asyncio.create_task(work())
        yield
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        app[key] -= 1
        if app[key] == 0:
            await model.close()

    app.cleanup_ctx.append(lifecycle)

    async def upload(request):
        if not request.content_type.startswith("multipart/"):
            raise ValueError("请用 multipart/form-data 上传 PDF，字段名为 file")
        # Read multipart chunks to disk: no request.read(), post() or whole-file bytes.
        reader = await request.multipart()
        with tempfile.TemporaryFile() as source:
            filename = None
            size = 0
            digest = hashlib.sha256()
            async for part in reader:
                if part.name != "file" or filename is not None:
                    raise ValueError("每次只能上传一个 PDF，字段名为 file")
                filename = (part.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
                if not filename.lower().endswith(".pdf") or not 5 <= len(filename) <= 180 or any(ord(c) < 32 for c in filename):
                    raise ValueError("请选择 PDF 文件，文件名长度须在 5 至 180 个字符之间")
                while chunk := await part.read_chunk(size=256 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    await file_io(source.write, chunk)
            if not filename or not size:
                raise ValueError("请选择非空的 PDF 文件")
            await file_io(validate_pdf, source)
            return await file_io(repository.create, source, filename, size, digest.hexdigest())

    async def download(request, identifier, *, image=False):
        if image:
            stream = await asyncio.to_thread(repository.open_image, identifier)
            filename, mime = "region-image.png", "image/png"
        else:
            doc = await asyncio.to_thread(repository.get, identifier)
            if doc is None:
                raise KeyError(identifier)
            stream, filename = await asyncio.to_thread(repository.open_file, identifier)
            # HTML snapshots are served as text attachments, never executable HTML.
            mime = "application/pdf" if doc.get("source_kind", "pdf") == "pdf" else "text/plain; charset=utf-8"
        disposition = "inline" if image or mime == "application/pdf" else "attachment"
        response = web.StreamResponse(headers={"Content-Type": mime, "Content-Length": str(stream.length),
            "Content-Disposition": f"{disposition}; filename=document; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff"})
        try:
            await response.prepare(request)
            while chunk := await file_io(stream.read, 256 * 1024):
                await response.write(chunk)
            await response.write_eof()
        except (ConnectionResetError, ClientConnectionError):
            # Closing the PDF viewer or cancelling a download is a normal client action.
            pass
        finally:
            await asyncio.to_thread(stream.close)
        return response

    async def import_url(request):
        payload = await request.json()
        if not isinstance(payload, dict) or set(payload) != {"url"}:
            raise ValueError("链接导入仅接受 url 字段")
        with tempfile.TemporaryFile() as source:
            document = await fetch_document(payload["url"], source)
            return await file_io(lambda: repository.create(source, document.filename, document.size, document.checksum,
                                  source_kind=document.kind, source_url=document.url, title=document.title))

    async def handle(request):
        try:
            identifier = request.match_info.get("identifier")
            action = request.match_info.get("action")
            if action in {"file", "image"}:
                return await download(request, identifier, image=action == "image")
            if action == "import-url":
                return web.json_response({"data": await import_url(request)}, status=201)
            if request.method == "GET":
                if identifier:
                    data = await asyncio.to_thread(repository.get, identifier)
                    if data is None:
                        raise KeyError(identifier)
                else:
                    page, size = int(request.query.get("page", 1)), int(request.query.get("page_size", 20))
                    if not 1 <= page <= 100000 or not 1 <= size <= 100:
                        raise ValueError("page 或 page_size 超出范围")
                    data = await asyncio.to_thread(repository.list, page, size, request.query.get("search", "")[:200], request.query.get("topic", "")[:80])
            elif request.method == "POST" and not identifier:
                return web.json_response({"data": await upload(request)}, status=201)
            else:
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise ValueError("请求体必须是 JSON 对象")
                revision = payload.pop("revision", None)
                if type(revision) is not int or revision < 1:
                    raise ValueError("请提供有效的 revision")
                if request.method == "PUT":
                    data = await asyncio.to_thread(repository.update, identifier, payload, revision)
                elif action == "parse":
                    if payload:
                        raise ValueError("重新解析仅接受 revision")
                    data = await asyncio.to_thread(repository.retry, identifier, revision)
                else:
                    if payload:
                        raise ValueError("删除仅接受 revision")
                    await asyncio.to_thread(repository.delete, identifier, revision)
                    data = {"deleted": True}
            return web.json_response({"data": data})
        except DatasetConflictError as exc:
            return web.json_response({"message": str(exc)}, status=409)
        except (KeyError, NoFile):
            return web.json_response({"message": "资料或原始文件不存在"}, status=404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"message": str(exc)}, status=400)
        except PyMongoError:
            return web.json_response({"message": "数据库暂不可用，请检查后端和 SSH 隧道"}, status=503)

    for prefix in ("/api/crowdSim", "/crowdSim"):
        path = prefix + "/" + profile.endpoint
        app.router.add_get(path, handle)
        app.router.add_post(path, handle)
        app.router.add_post(path + "/{action:import-url}", handle)
        app.router.add_get(path + "/{identifier}", handle)
        app.router.add_put(path + "/{identifier}", handle)
        app.router.add_delete(path + "/{identifier}", handle)
        app.router.add_get(path + "/{identifier}/{action:file|image}", handle)
        app.router.add_post(path + "/{identifier}/{action:parse}", handle)
    return repository
