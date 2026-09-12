from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database.db import create_pool, load_schemas
from .database.errors import install_error_handlers
from .routes.users import router as users_router
from .routes.organisations import router as orginisations_router
from .routes.venues import router as venues_router
from .routes.events import router as events_router
from .routes.bookings import router as bookings_router


def create_api():
    @asynccontextmanager
    async def lifecycle(app):
        await load_schemas()
        app.state.pool = await create_pool()
        try:
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(lifespan=lifecycle)
    install_error_handlers(app)

    @app.get("/health")
    def health():
        return "ok"

    app.include_router(users_router, tags=["users"])
    app.include_router(orginisations_router, tags=["organisations"])
    app.include_router(venues_router, tags=["venues"])
    app.include_router(events_router, tags=["events"])
    app.include_router(bookings_router, tags=["bookings"])

    return app
