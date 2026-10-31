"""Case CRUD and isolated, versioned HTML detail pages; no simulation imports."""
import asyncio
import json
import logging
from tempfile import SpooledTemporaryFile
from urllib.parse import quote

from aiohttp import web
from gridfs.errors import NoFile
from pymongo.errors import PyMongoError

from .datasets import parse_json
from ..repositories.cases import MAX_PAGE_BYTES, CaseRepository
from ..repositories.datasets import DatasetConflictError

# Uploaded scripts can render their page but cannot access the parent app's origin,
# cookies/storage, submit forms, or navigate the app. The iframe adds the same sandbox.
PAGE_CSP = ("sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; "
            "default-src 'none'; script-src 'unsafe-inline' https:; style-src 'unsafe-inline' https:; "
            "img-src data: blob: https: http:; media-src https: http: blob:; font-src data: https:; "
            "frame-src https:; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")


def revision_value(value):
    if type(value) is not int or value < 1:
        raise ValueError('请提供有效 revision')
    return value


def register_case_routes(app, database):
    repository = CaseRepository(database)
    ready = asyncio.Event()

    async def lifecycle(app):
        async def initialize():
            while True:
                try:
                    await asyncio.to_thread(repository.initialize)
                    ready.set()
                    return
                except PyMongoError:
                    logging.getLogger(__name__).warning('案例库初始化等待数据库连接')
                    await asyncio.sleep(3)
        task = asyncio.create_task(initialize())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    app.cleanup_ctx.append(lifecycle)

    async def upload(request, identifier):
        if request.content_type != 'multipart/form-data':
            raise ValueError('详情页上传须使用 multipart/form-data')
        reader = await request.multipart()
        filename, revision = None, None
        with SpooledTemporaryFile(max_size=1024 * 1024) as source:
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == 'file' and filename is None and part.filename:
                    filename = part.filename
                    size = 0
                    while chunk := await part.read_chunk(64 * 1024):
                        size += len(chunk)
                        if size > MAX_PAGE_BYTES:
                            raise ValueError('HTML 详情页不能超过 16 MiB')
                        source.write(chunk)
                elif part.name == 'revision' and revision is None:
                    raw = await part.read_chunk(128)
                    if not part.at_eof():
                        raise ValueError('revision 无效')
                    revision = revision_value(int(raw.decode('ascii')))
                else:
                    raise ValueError('上传仅接受一个 file 和 revision')
            if not filename or revision is None:
                raise ValueError('上传须包含 file 和 revision')
            return await asyncio.to_thread(repository.attach_page, identifier, revision, source, filename)

    async def handle(request):
        if not ready.is_set():
            return web.json_response({'message': '案例库正在连接数据库，请稍后刷新'}, status=503)
        try:
            identifier = request.match_info.get('identifier')
            page_id = request.match_info.get('page_id')
            if page_id:
                def read_page():
                    stream, filename = repository.open_page(identifier, page_id)
                    with stream:
                        return stream.read(), filename
                raw, filename = await asyncio.to_thread(read_page)
                disposition = 'attachment' if request.query.get('download') == '1' else 'inline'
                return web.Response(body=raw, content_type='text/html', charset='utf-8', headers={
                    'Content-Security-Policy': PAGE_CSP, 'X-Content-Type-Options': 'nosniff',
                    'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                    'Content-Disposition': f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}",
                })
            if request.method == 'GET':
                data = await asyncio.to_thread(repository.get, identifier) if identifier else await asyncio.to_thread(repository.list)
                if data is None:
                    raise KeyError(identifier)
            elif request.path.endswith('/pages'):
                data = await upload(request, identifier)
            else:
                payload = await request.json(loads=parse_json)
                if not isinstance(payload, dict):
                    raise ValueError('请求体必须为 JSON 对象')
                revision = payload.pop('revision', None)
                if request.method in ('PUT', 'DELETE'):
                    revision_value(revision)
                if request.path.endswith('/links'):
                    data = await asyncio.to_thread(repository.set_links, identifier, revision, payload)
                elif request.method == 'DELETE':
                    if payload:
                        raise ValueError('删除仅接受 revision')
                    await asyncio.to_thread(repository.delete, identifier, revision)
                    data = {'deleted': True}
                else:
                    data = await asyncio.to_thread(repository.save, payload, identifier, revision)
            return web.json_response({'data': data}, status=201 if request.method == 'POST' else 200)
        except DatasetConflictError as exc:
            return web.json_response({'message': str(exc)}, status=409)
        except (KeyError, NoFile):
            return web.json_response({'message': '案例或详情页不存在，或已删除'}, status=404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except PyMongoError:
            return web.json_response({'message': '数据库暂不可用，请检查数据服务和数据库连接'}, status=503)

    for prefix in ('/api/crowdSim', '/crowdSim'):
        root = prefix + '/cases'
        app.router.add_get(root, handle)
        app.router.add_post(root, handle)
        app.router.add_get(root + '/{identifier}', handle)
        app.router.add_put(root + '/{identifier}', handle)
        app.router.add_delete(root + '/{identifier}', handle)
        app.router.add_put(root + '/{identifier}/links', handle)
        app.router.add_post(root + '/{identifier}/pages', handle)
        app.router.add_get(root + '/{identifier}/pages/{page_id}', handle)
    return repository
