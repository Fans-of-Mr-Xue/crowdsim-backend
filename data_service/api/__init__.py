"""Compose data HTTP routes without importing simulation modules."""
from aiohttp import web

from .datasets import register_database_routes
from .emergency_plans import register_plan_routes
from .regulations import register_regulation_routes


def create_app(repository=None, *, model=None):
    app = web.Application(client_max_size=8 * 1024 * 1024)
    repository = register_database_routes(app, repository)
    register_plan_routes(app, repository.db, model=model)
    register_regulation_routes(app, repository.db, model=model)
    return app
