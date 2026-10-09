"""Compose data HTTP routes without importing simulation modules."""
from aiohttp import web

from .datasets import register_database_routes
from .emergency_plans import register_plan_routes
from .regulations import register_regulation_routes
from .research_documents import register_research_routes
from .news import register_news_routes
from .environments import register_environment_routes
from .models import register_model_routes


def create_app(repository=None, *, model=None):
    app = web.Application(client_max_size=8 * 1024 * 1024)
    repository = register_database_routes(app, repository)
    register_model_routes(app)
    register_environment_routes(app, repository.db)
    register_news_routes(app, repository.db, model=model)
    register_research_routes(app, repository.db, model=model)
    register_plan_routes(app, repository.db, model=model)
    register_regulation_routes(app, repository.db, model=model)
    return app
