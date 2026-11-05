"""Compose data HTTP routes without importing simulation modules."""
from aiohttp import web

from .datasets import register_database_routes
from .emergency_plans import register_plan_routes
from .regulations import register_regulation_routes
from .research_documents import register_research_routes
from .news import register_news_routes
from .environments import register_environment_routes
from .subjects import register_subject_routes
from .cases import register_case_routes
from .models import register_model_routes
from .analysis_tasks import register_analysis_task_routes
from .domain_models import register_domain_model_routes
from .domain_portrait import register_domain_portrait_routes
from .pre_assessments import register_pre_assessment_routes
from .page_assistant import register_page_assistant_routes
from ..services.task_roads import TaskRoadService
from ..repositories.environments import EnvironmentRepository
from .model_gateway import register_model_gateway_routes


def create_app(repository=None, *, model=None):
    from crowdsim.during_models import register_during_model_routes
    app = web.Application(client_max_size=8 * 1024 * 1024)
    repository = register_database_routes(app, repository)
    main_gateway = register_model_gateway_routes(app, include_model_routes=False)
    register_model_routes(app, main_gateway=main_gateway)
    register_during_model_routes(app)
    register_page_assistant_routes(app, repository.db)
    register_environment_routes(app, repository.db)
    register_subject_routes(app, repository.db)
    register_case_routes(app, repository.db)
    task_roads = TaskRoadService(repository.db, EnvironmentRepository(repository.db))
    register_analysis_task_routes(app, repository.db, task_roads=task_roads)
    register_domain_model_routes(app, repository.db, task_roads=task_roads)
    register_domain_portrait_routes(app, repository.db, task_roads=task_roads)
    register_pre_assessment_routes(app, repository.db, task_roads=task_roads)
    register_news_routes(app, repository.db, model=model, task_roads=task_roads)
    register_research_routes(app, repository.db, model=model)
    register_plan_routes(app, repository.db, model=model)
    register_regulation_routes(app, repository.db, model=model)
    return app
