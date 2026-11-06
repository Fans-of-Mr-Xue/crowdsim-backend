"""Model selection support without changing simulation or shared user defaults."""
import json
from aiohttp import web
from .datasets import parse_json
from ..connectors.models import ModelGateway, MODEL_SPECS
from ..connectors.llm import ModelError
from .model_gateway import GatewayUpstreamError, read_json


def selected_request_model(request):
    identifier = request.headers.get('X-CrowdSim-Model', '')
    # New requests share the main menu's 8800 identity. Persisted provider IDs
    # remain unchanged in model_configs/for_provider for historical tasks.
    identifier = {'GLM-5.3-Flash': 'GLM-5.3-Flash-8800',
                  'GLM-5.3-Flash-Tabby': 'GLM-5.3-Flash'}.get(identifier, identifier)
    if identifier and identifier not in {item[0] for item in MODEL_SPECS} | {'GLM-5.3-Flash'}:
        raise ValueError('请选择已配置的模型')
    return identifier


def register_model_routes(app, *, main_gateway=None, gateway=None):
    gateway = gateway or ModelGateway()
    async def lifecycle(app):
        await gateway.open()
        try:
            yield
        finally:
            await gateway.close()
    app.cleanup_ctx.append(lifecycle)

    async def status(request):
        if main_gateway is not None and '/pre/models/' not in request.path:
            items = await main_gateway.statuses()
        else:
            items = await gateway.status()
        return web.json_response({'data': items}, headers={'Cache-Control': 'no-store'})

    async def chat(request):
        try:
            identifier = request.match_info['model_id']
            if main_gateway is not None and '/pre/models/' not in request.path:
                payload = await read_json(request)
                data = await main_gateway.chat(payload, identifier)
            else:
                payload = await request.json(loads=parse_json)
                identifier = 'GLM-5.3-Flash' if identifier == 'GLM-5.3-Flash-Tabby' else identifier
                data = await gateway.chat(identifier, payload)
            return web.json_response(data, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except GatewayUpstreamError as exc:
            return web.json_response({'message': str(exc)}, status=exc.status)
        except ModelError as exc:
            return web.json_response({'message': str(exc)}, status=502)

    # Legacy pre-only IDs remain reachable without shadowing shared routes.
    for prefix in ('/api/crowdSim', '/crowdSim', '/api/crowdSim/pre', '/crowdSim/pre'):
        app.router.add_get(prefix + '/models/status', status)
        app.router.add_post(prefix + '/models/{model_id}/chat/completions', chat)
