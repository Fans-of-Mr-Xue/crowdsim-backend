"""Asynchronous CPU workers; task definitions are read from Mongo, never the UI."""
import asyncio
import logging
import multiprocessing
import json
import tempfile
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from uuid import uuid4
from aiohttp import web
from pymongo.errors import PyMongoError
from .datasets import parse_json
from ..prediction.catalog import method_catalog
from ..prediction.inputs import default_settings, normalize_methods, normalize_settings
from ..repositories.analysis_tasks import AnalysisTaskRepository
from ..repositories.datasets import DatasetConflictError
from ..repositories.pre_assessments import PreAssessmentRepository, input_key
from ..services.task_roads import TaskRoadService
from ..connectors.geography import GeographyError

logger = logging.getLogger(__name__)


def compute(task, roads, settings, methods, progress_path=None):
    # Heavy optional libraries only load in the computation process.
    from ..prediction.solver import compute_comparison
    last = 0.
    def report(state):
        nonlocal last
        current = time.monotonic()
        if not progress_path or (state['minute'] != 0 and current - last < 1):
            return
        last = current
        path = Path(progress_path)
        try:
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state), encoding='utf-8')
            temporary.replace(path)
        except FileNotFoundError:
            # The parent is shutting down and has removed its private directory.
            pass
    return compute_comparison(task, roads, settings, methods, progress=report)


def register_pre_assessment_routes(app, database, *, task_roads=None):
    tasks, runs = AnalysisTaskRepository(database), PreAssessmentRepository(database)
    road_service = task_roads or TaskRoadService(database, tasks.environments)
    submissions = asyncio.Lock()
    owner = uuid4().hex
    workers, pending = {}, set()
    slots = asyncio.Semaphore(2)

    async def lifecycle(application):
        workers['pool'] = ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('spawn'))
        async def initialize():
            while True:
                try:
                    await asyncio.to_thread(runs.initialize)
                    await asyncio.to_thread(road_service.snapshots.initialize)
                    return
                except PyMongoError:
                    await asyncio.sleep(3)
        initialization = asyncio.create_task(initialize())
        yield
        initialization.cancel()
        for job in list(pending):
            job.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(initialization, return_exceptions=True)
        await asyncio.to_thread(workers['pool'].shutdown, wait=True, cancel_futures=True)
        try:
            await asyncio.to_thread(runs.interrupt_owner, owner)
        except PyMongoError:
            logger.exception('Unable to mark interrupted forecasting jobs')
    app.cleanup_ctx.append(lifecycle)

    async def execute(identifier, task, roads, settings, methods):
        progress_dir = tempfile.TemporaryDirectory(prefix='crowdsim-progress-')
        progress_path = Path(progress_dir.name) / 'progress.json'
        async def heartbeat():
            last_lease = time.monotonic()
            while True:
                await asyncio.sleep(2)
                try:
                    state = json.loads(await asyncio.to_thread(progress_path.read_text, encoding='utf-8'))
                    await asyncio.to_thread(runs.update, identifier, 'running', progress=state)
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                except PyMongoError:
                    logger.warning('Forecast progress persistence unavailable for %s; retrying', identifier)
                if time.monotonic() - last_lease >= 30:
                    try:
                        await asyncio.to_thread(runs.heartbeat, identifier)
                        last_lease = time.monotonic()
                    except PyMongoError:
                        logger.warning('Forecast lease persistence unavailable for %s; retrying', identifier)
        lease = asyncio.create_task(heartbeat())
        try:
            async with slots:
                await asyncio.to_thread(runs.update, identifier, 'running')
                result = await asyncio.get_running_loop().run_in_executor(workers['pool'], compute, task, roads, settings, methods, str(progress_path))
                await asyncio.to_thread(runs.complete, identifier, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception('Forecast run %s failed', identifier)
            message = str(exc) if isinstance(exc, ValueError) else '预测计算失败，请检查数据服务日志后重新计算'
            try:
                await asyncio.to_thread(runs.update, identifier, 'failed', error=message)
            except PyMongoError:
                logger.exception('Unable to persist forecasting failure')
        finally:
            lease.cancel()
            await asyncio.gather(lease, return_exceptions=True)
            progress_dir.cleanup()

    async def handle(request):
        try:
            identifier = request.match_info.get('identifier')
            if request.path.endswith('/formula-preview'):
                from ..prediction.custom_formulas import preview_formula
                payload = await request.json(loads=parse_json)
                if not isinstance(payload, dict) or set(payload) != {'method', 'v0', 'rho_max'}:
                    raise ValueError('公式预览须提供method、v0和rho_max')
                data = await asyncio.to_thread(preview_formula, payload['method'], payload['v0'], payload['rho_max'])
                return web.json_response({'data': data}, headers={'Cache-Control': 'no-store'})
            if request.path.endswith('/catalog'):
                from ..prediction.consultation_catalog import consultation_catalog
                return web.json_response({'data': consultation_catalog()}, headers={'Cache-Control': 'no-store'})
            if request.path.endswith('/methods'):
                task = None
                if request.query.get('task_id'):
                    task = await asyncio.to_thread(tasks.get, request.query['task_id'])
                    if task is None:
                        raise KeyError('task')
                return web.json_response({'data': method_catalog(task)}, headers={'Cache-Control': 'no-store'})
            if request.path.endswith('/road-network'):
                payload = await request.json(loads=parse_json)
                if not isinstance(payload, dict) or set(payload) != {'task_id', 'task_revision'}:
                    raise ValueError('路网更新仅接受 task_id 和 task_revision')
                task = await asyncio.to_thread(tasks.get, payload['task_id'])
                if task is None:
                    raise KeyError('task')
                if type(payload['task_revision']) is not int or payload['task_revision'] != task['revision']:
                    raise DatasetConflictError('任务版本已变化，请重新读取任务')
                _, snapshot = await road_service.resolve(task, refresh=True)
                return web.json_response({'data': {'status': 'saved', **snapshot}})
            if request.method == 'POST':
                payload = await request.json(loads=parse_json)
                if not isinstance(payload, dict) or set(payload) - {'task_id', 'task_revision', 'methods', 'settings', 'force'}:
                    raise ValueError('仅接受 task_id、task_revision、methods、settings 和 force，任务定义由后端读取')
                if type(payload.get('force', False)) is not bool:
                    raise ValueError('force 必须为布尔值')
                if not isinstance(payload.get('task_id'), str) or not payload['task_id']:
                    raise ValueError('请选择数据库中的分析任务')
                task = await asyncio.to_thread(tasks.get, payload['task_id'])
                if task is None:
                    raise KeyError(payload['task_id'])
                if type(payload.get('task_revision')) is not int or payload['task_revision'] != task['revision']:
                    raise DatasetConflictError('任务版本已变化，请重新读取任务再计算')
                settings = normalize_settings(task, payload.get('settings'))
                selected = task.get('components', {}).get('research_plan', {}).get('experiment', {}).get('model_ids')
                methods = normalize_methods(payload.get('methods', selected), task)
                async with submissions:
                    roads, source = {'features': []}, {'id': 'boundary-assumption'}
                    if not settings['allow_boundary_walkable']:
                        roads, source = await road_service.resolve(task)
                    key = input_key(task, settings, methods, source['id'])
                    if not payload.get('force', False):
                        cached = await asyncio.to_thread(runs.reusable, key)
                        if cached:
                            return web.json_response({'data': {**cached, 'reused': True}}, status=200)
                    if len(pending) >= 6:
                        return web.json_response({'message': '预测队列已满，请稍后重试'}, status=429)
                    record = await asyncio.to_thread(runs.create, task, settings, methods, owner, key, source)
                    # Freeze the task roads with this result; live environment edits cannot change it.
                    roads['snapshot'] = source
                    job = asyncio.create_task(execute(record['id'], task, roads, settings, methods))
                    pending.add(job); job.add_done_callback(pending.discard)
                    return web.json_response({'data': record}, status=202)
            if request.path.endswith('/context'):
                task = await asyncio.to_thread(tasks.get, request.query.get('task_id', ''))
                if task is None:
                    raise KeyError('task')
                road_status = await road_service.describe(task)
                data = {'task': {**task, 'road_network': road_status}, 'settings': default_settings(task), 'roads': road_status, 'methods': method_catalog(task), 'selected_methods': task.get('components', {}).get('research_plan', {}).get('experiment', {}).get('model_ids', [])}
            elif request.path.endswith('/latest'):
                scope = request.query.get('scope', 'task')
                prefer = request.query.get('prefer', 'active')
                if scope not in ('task', 'current') or prefer not in ('active', 'completed'):
                    raise ValueError('scope 仅接受 task/current，prefer 仅接受 active/completed')
                task = await asyncio.to_thread(tasks.get, request.query.get('task_id', ''))
                if task is None:
                    raise KeyError('task')
                data = await asyncio.to_thread(runs.latest, task['id'], task['revision'] if scope == 'current' else None,
                                               prefer_completed=prefer == 'completed')
            elif request.path.endswith('/result'):
                data = await asyncio.to_thread(runs.read_result, identifier)
            else:
                data = await asyncio.to_thread(runs.get, identifier)
                if data is None:
                    raise KeyError(identifier)
            return web.json_response({'data': data}, headers={'Cache-Control': 'no-store'})
        except KeyError:
            return web.json_response({'message': '任务或预测结果不存在'}, status=404)
        except DatasetConflictError as exc:
            return web.json_response({'message': str(exc)}, status=409)
        except GeographyError as exc:
            return web.json_response({'message': str(exc)}, status=502)
        except (ValueError, TypeError) as exc:
            return web.json_response({'message': str(exc)}, status=400)
        except PyMongoError:
            return web.json_response({'message': '数据库暂不可用，未确认保存预测结果'}, status=503)

    for prefix in ('/api/crowdSim', '/crowdSim'):
        root = prefix + '/pre-assessments'
        for suffix in ('catalog', 'methods', 'context', 'latest'):
            app.router.add_get(root + '/' + suffix, handle)
        app.router.add_post(root, handle)
        app.router.add_post(root + '/road-network', handle)
        app.router.add_post(root + '/formula-preview', handle)
        app.router.add_get(root + '/{identifier}', handle)
        app.router.add_get(root + '/{identifier}/result', handle)
    return runs
