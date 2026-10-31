"""Generate declarative page edits; the browser owns actual form/business actions."""
import asyncio
import json
from uuid import uuid4
from aiohttp import web
from pymongo.errors import PyMongoError
from .datasets import parse_json
from .models import selected_request_model
from ..connectors.models import ModelGateway
from ..connectors.llm import ModelError
from ..repositories.page_assistant import PageAssistantRepository
from ..repositories.datasets import DatasetConflictError
from ..schemas.page_assistant import identifier, validate_context, finite_json
from ..schemas.assistant_confirmations import resolve_decision, decision_text
from ..services.page_assistant import PageAssistantService


def register_page_assistant_routes(app, database):
    repository, gateway = PageAssistantRepository(database), ModelGateway()
    service = PageAssistantService(database, gateway)
    slots = asyncio.Semaphore(3)

    async def lifecycle(app):
        await gateway.open()
        async def initialize():
            while True:
                try:
                    await asyncio.to_thread(repository.initialize)
                    return
                except PyMongoError:
                    await asyncio.sleep(3)
        initialization = asyncio.create_task(initialize())
        yield
        initialization.cancel()
        await asyncio.gather(initialization, return_exceptions=True)
        await gateway.close()
    app.cleanup_ctx.append(lifecycle)

    async def handle(request):
        try:
            payload = await request.json(loads=parse_json) if request.method == 'POST' else dict(request.query)
            if not isinstance(payload, dict):
                raise ValueError('请求必须为 JSON 对象')
            client = identifier(payload.get('client_id'))
            thread_id = request.match_info.get('identifier')
            if thread_id:
                identifier(thread_id)
            action = request.match_info.get('action')
            if request.method == 'GET':
                data = await asyncio.to_thread(repository.get, client, thread_id) if thread_id else await asyncio.to_thread(repository.list, client, payload.get('scope'))
            elif request.method == 'DELETE':
                data = await asyncio.to_thread(repository.delete, client, thread_id)
            elif not thread_id:
                content, context = payload.get('content'), validate_context(payload.get('context'))
                if context.get('example_mode') or context['page'] != 'task-definition':
                    raise ValueError('仅真实需求对话可创建历史记录')
                if not isinstance(content, str) or not 1 <= len(content.strip()) <= 8000:
                    raise ValueError('请先发送有效消息；空对话不保存')
                if payload.get('decision'):
                    raise ValueError('请先发送消息，再确认助手建议')
                definition = None
                if payload.get('definition') is not None:
                    from ..schemas.analysis_tasks import validate_task
                    raw = payload['definition']
                    if not isinstance(raw, dict):
                        raise ValueError('需求草稿格式无效')
                    try:
                        definition = validate_task({**raw, 'title': raw.get('title') or '未命名需求', 'status': 'draft'}, allow_unbounded_draft=True)
                        definition['title'] = raw.get('title') or ''
                        if definition['region'].get('mode') == 'environment' or definition['region'].get('geometry') is not None:
                            definition['region'] = await asyncio.to_thread(service.tasks.region_snapshot, definition['region'])
                    except ValueError:
                        # An optional starting snapshot may still lack a
                        # boundary. It must not prevent the first conversation.
                        definition = None
                provider = selected_request_model(request) or 'GLM-5.3-Flash-8800'
                async with slots:
                    plan = await service.generate([], content.strip(), context, provider)
                    data = await asyncio.to_thread(repository.create_completed, client, content.strip(), context, plan, provider, definition)
            elif action == 'title':
                data = await asyncio.to_thread(repository.rename, client, thread_id, payload.get('title'))
            elif action == 'draft':
                from ..schemas.analysis_tasks import validate_task
                raw = payload.get('definition')
                if not isinstance(raw, dict):
                    raise ValueError('需求草稿格式无效')
                # Conversation drafts may precede naming a formal task.
                definition = validate_task({**raw, 'title': raw.get('title') or '未命名需求', 'status': raw.get('status', 'draft')}, allow_unbounded_draft=True)
                definition['title'] = raw.get('title') or ''
                definition['status'] = raw.get('status') if raw.get('status') in ('draft', 'confirmed') else 'draft'
                if definition['region'].get('mode') == 'environment' or definition['region'].get('geometry') is not None:
                    definition['region'] = await asyncio.to_thread(service.tasks.region_snapshot, definition['region'])
                reference = payload.get('task_reference')
                if reference:
                    await service.read_task(reference)
                data = await asyncio.to_thread(repository.draft, client, thread_id, definition, reference)
            elif action == 'execution':
                turn_id = identifier(payload.get('turn_id'))
                result = payload.get('result')
                if not isinstance(result, dict) or set(result) != {'status', 'logs'} or result.get('status') not in ('applied', 'skipped', 'failed', 'partial', 'answered') or not isinstance(result.get('logs'), list) or any(not isinstance(log, str) for log in result['logs']) or len(json.dumps(result, ensure_ascii=False)) > 16000:
                    raise ValueError('页面执行结果无效')
                data = await asyncio.to_thread(repository.execution, client, thread_id, turn_id, result)
            elif action == 'messages':
                content, context, revision = payload.get('content'), validate_context(payload.get('context')), payload.get('revision')
                if context.get('example_mode'):
                    raise ValueError('示例只能使用独立示例接口，不写入正式对话')
                if not isinstance(content, str) or not 1 <= len(content.strip()) <= 8000 or type(revision) is not int or revision < 1:
                    raise ValueError('消息须为 1–8000 字文本，并提供会话 revision')
                provider = selected_request_model(request) or 'GLM-5.3-Flash-8800'
                turn_id = uuid4().hex
                async with slots:
                    thread = await asyncio.to_thread(repository.begin, client, thread_id, revision, turn_id)
                    try:
                        decision = resolve_decision(thread['messages'], payload.get('decision'))
                        if decision:
                            content = decision_text(decision)
                        plan = await service.generate(thread['messages'], content.strip(), context, provider, decision)
                        data = await asyncio.to_thread(repository.complete, client, thread_id, turn_id, content.strip(), context, plan, provider, decision)
                    finally:
                        await asyncio.to_thread(repository.release, client, thread_id, turn_id)
            else:
                raise ValueError('不支持的会话操作')
            return web.json_response({'data': data}, headers={'Cache-Control': 'no-store'})
        except KeyError:
            return web.json_response({'message': '助手会话不存在'}, status=404)
        except DatasetConflictError as exc:
            return web.json_response({'message': str(exc)}, status=409)
        except ModelError as exc:
            return web.json_response({'message': str(exc)}, status=502)
        except (ValueError, TypeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except PyMongoError:
            return web.json_response({'message': '助手会话数据库暂不可用，请检查数据库连接'}, status=503)

    async def example_message(request):
        try:
            payload = await request.json(loads=parse_json)
            if not isinstance(payload, dict):
                raise ValueError('示例请求无效')
            context = validate_context(payload.get('context'))
            if context['page'] != 'task-definition' or not context.get('example_mode'):
                raise ValueError('仅需求界定示例可使用此接口')
            if context.get('task_reference') or (context.get('state', {}).get('task-definition') or {}).get('task_id'):
                raise ValueError('示例不能绑定真实任务')
            history = payload.get('messages', [])
            finite_json(history)
            if not isinstance(history, list) or len(history) > 40 or len(json.dumps(history, ensure_ascii=False)) > 180000:
                raise ValueError('示例历史过长，请重新打开示例')
            if any(not isinstance(row, dict) or row.get('role') not in ('user', 'assistant') or not isinstance(row.get('content'), str) for row in history):
                raise ValueError('示例历史格式无效')
            content = payload.get('content')
            if not isinstance(content, str) or not 1 <= len(content.strip()) <= 8000:
                raise ValueError('消息须为1至8000字')
            decision = resolve_decision(history, payload.get('decision'))
            if decision:
                content = decision_text(decision)
            provider = selected_request_model(request) or 'GLM-5.3-Flash-8800'
            async with slots:
                plan = await service.generate(history, content.strip(), context, provider, decision)
            return web.json_response({'data': {'id': uuid4().hex, 'role': 'assistant', 'content': plan['reply'], 'page': 'task-definition', 'plan': plan, 'exampleDemo': True}}, headers={'Cache-Control': 'no-store'})
        except DatasetConflictError as exc:
            return web.json_response({'message': str(exc)}, status=409)
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except ModelError as exc:
            return web.json_response({'message': str(exc)}, status=502)
        except PyMongoError:
            return web.json_response({'message': '知识库暂不可用，请检查数据库连接'}, status=503)

    for prefix in ('/api/crowdSim', '/crowdSim'):
        async def search_cases(request):
            try:
                payload = await request.json(loads=parse_json)
                query = payload.get('query', '')
                if not isinstance(query, str) or not 1 <= len(query.strip()) <= 4000:
                    raise ValueError('请提供1至4000字的案例检索描述')
                data = await asyncio.to_thread(service.case_search.search, query)
                return web.json_response({'data': data}, headers={'Cache-Control': 'no-store'})
            except (ValueError, TypeError, AttributeError):
                return web.json_response({'message': '案例检索描述无效'}, status=400)
            except PyMongoError:
                return web.json_response({'message': '案例库暂不可用，请检查数据库连接'}, status=503)
        app.router.add_post(prefix + '/requirements/cases/search', search_cases)
        app.router.add_post(prefix + '/page-assistant/example/messages', example_message)
        root = prefix + '/page-assistant/conversations'
        app.router.add_get(root, handle)
        app.router.add_post(root, handle)
        app.router.add_get(root + '/{identifier}', handle)
        app.router.add_delete(root + '/{identifier}', handle)
        app.router.add_post(root + '/{identifier}/{action:messages|execution|draft|title}', handle)
