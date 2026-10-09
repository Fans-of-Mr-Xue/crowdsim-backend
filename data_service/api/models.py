"""Model selection support without changing simulation or shared user defaults."""
import json
from aiohttp import web
from .datasets import parse_json
from ..connectors.models import ModelGateway, MODEL_SPECS
from ..connectors.llm import ModelError


def selected_request_model(request):
    identifier = request.headers.get('X-CrowdSim-Model', '')
    if identifier and identifier not in {item[0] for item in MODEL_SPECS}:
        raise ValueError('请选择已配置的模型')
    return identifier


def register_model_routes(app):
    gateway = ModelGateway()
    async def lifecycle(app):
        await gateway.open()
        try:
            yield
        finally:
            await gateway.close()
    app.cleanup_ctx.append(lifecycle)

    async def status(request):
        return web.json_response({'data': await gateway.status()}, headers={'Cache-Control': 'no-store'})

    async def chat(request):
        try:
            payload = await request.json(loads=parse_json)
            data = await gateway.chat(request.match_info['model_id'], payload)
            return web.json_response(data, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except ModelError as exc:
            return web.json_response({'message': str(exc)}, status=502)

    for prefix in ('/api/crowdSim', '/crowdSim'):
        app.router.add_get(prefix + '/models/status', status)
        app.router.add_post(prefix + '/models/{model_id}/chat/completions', chat)
