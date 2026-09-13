from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from .database.db import create_db_pool, load_schemas
from .cache.cache import create_cache_client
from .database.errors import install_error_handlers
from .routes.users import router as users_router
from .routes.organisations import router as orginisations_router
from .routes.venues import router as venues_router
from .routes.events import router as events_router
from .routes.bookings import router as bookings_router


def create_api():
    @asynccontextmanager
    async def lifecycle(app):
        async with AsyncExitStack() as stack:
            await load_schemas()
            db_pool = await create_db_pool()
            stack.push_async_callback(db_pool.close)
            cache = create_cache_client()
            stack.push_async_callback(cache.aclose)
            yield {"db_pool": db_pool, "cache": cache}

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
