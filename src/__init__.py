from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database.db import load_schemas
from .database.errors import install_error_handlers
from .routes.users import router as users_router
from .routes.organisations import router as orginisations_router
from .routes.venues import router as venues_router


def create_api():
    @asynccontextmanager
    async def lifecycle(app):
        await load_schemas()
        yield

    app = FastAPI(lifespan=lifecycle)
    install_error_handlers(app)

    @app.get("/health")
    def health():
        return "ok"

    app.include_router(users_router)
    app.include_router(orginisations_router)
    app.include_router(venues_router)

    return app
