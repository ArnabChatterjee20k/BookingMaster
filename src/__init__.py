from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database.db import load_schemas
from .routes.users import router as users_router


def create_api():
    @asynccontextmanager
    async def lifecycle(app):
        await load_schemas()
        yield

    app = FastAPI(lifespan=lifecycle)

    @app.get("/health")
    def health():
        return "ok"

    app.include_router(users_router)

    return app
