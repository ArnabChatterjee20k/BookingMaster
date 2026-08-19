from fastapi import FastAPI
from .database.db import load_schemas
from contextlib import asynccontextmanager

def create_api():
    @asynccontextmanager
    async def lifecycle(app):
        await load_schemas()
        yield

    app = FastAPI(lifespan=lifecycle)

    @app.get("/health")
    def health():
        return "ok"

    return app