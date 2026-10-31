"""Task-scoped bootstrap and explicit user confirmation of domain models."""
import asyncio
import json
from aiohttp import web
from pymongo.errors import PyMongoError
from .datasets import parse_json
from ..repositories.datasets import DatasetConflictError
from ..services.domain_models import DomainModelService


def register_domain_model_routes(app, database, *, task_roads):
    service = DomainModelService(database)

    async def handle(request):
        try:
            payload = await request.json(loads=parse_json)
            if not isinstance(payload, dict) or set(payload) - {'revision', 'definition'} or type(payload.get('revision')) is not int or payload['revision'] < 1:
                raise ValueError('请提供有效的任务 revision')
            identifier = request.match_info['identifier']
            if request.match_info['action'] == 'initialize':
                if set(payload) != {'revision'}:
                    raise ValueError('初始化只接受任务 revision')
                result = await asyncio.to_thread(service.initialize, identifier, payload['revision'])
            else:
                definition = payload.get('definition')
                if definition is not None and not isinstance(definition, dict):
                    raise ValueError('领域模型定义须为 JSON 对象')
                result = {'task': await asyncio.to_thread(service.confirm, identifier, payload['revision'], definition)}
            result['task'] = await task_roads.task_context(result['task'])
            return web.json_response({'data': result}, headers={'Cache-Control': 'no-store'})
        except KeyError:
            return web.json_response({'message': '分析任务不存在'}, status=404)
        except DatasetConflictError as exc:
            return web.json_response({'message': str(exc)}, status=409)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except PyMongoError:
            return web.json_response({'message': '数据库暂不可用，领域模型未确认保存'}, status=503)

    for prefix in ('/api/crowdSim', '/crowdSim'):
        app.router.add_post(prefix + '/analysis-tasks/{identifier}/domain-model/{action:initialize|confirm}', handle)
