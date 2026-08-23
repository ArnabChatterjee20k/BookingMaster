"""Translate database errors into HTTP responses in one place.

Routes stay free of try/except around every query -- they let asyncpg raise and
this handler decides the status code. Registered on the app rather than the
router because Starlette resolves exception handlers app-wide; APIRouter has no
add_exception_handler of its own.
"""

import logging

from asyncpg import exceptions as pg
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Most specific first -- resolution walks this in order.
# Everything below descends from pg.PostgresError, so one registration catches
# them all and this table only decides which status each deserves.
_RULES: list[tuple[type[Exception], int, str]] = [
    (pg.UniqueViolationError, status.HTTP_409_CONFLICT, "resource already exists"),
    (pg.ForeignKeyViolationError, status.HTTP_409_CONFLICT, "referenced resource does not exist"),
    (pg.NotNullViolationError, status.HTTP_400_BAD_REQUEST, "missing required field"),
    (pg.CheckViolationError, status.HTTP_400_BAD_REQUEST, "invalid value"),
    (pg.DataError, status.HTTP_400_BAD_REQUEST, "malformed value"),
]

_FALLBACK = (status.HTTP_500_INTERNAL_SERVER_ERROR, "internal error")


def _resolve(exc: Exception) -> tuple[int, str]:
    for kind, code, message in _RULES:
        if isinstance(exc, kind):
            return code, message
    return _FALLBACK


async def handle_database_error(request: Request, exc: Exception) -> JSONResponse:
    code, message = _resolve(exc)
    if code >= 500:
        # An unmapped error is a bug (bad SQL, missing column) -- keep the traceback.
        logger.exception("database error on %s %s", request.method, request.url.path)
    else:
        logger.warning(
            "%s on %s %s: %s", type(exc).__name__, request.method, request.url.path, exc
        )
    # Never echo `exc` to the client: asyncpg messages carry table, column and
    # constraint names, and for a unique violation the conflicting value itself.
    return JSONResponse({"detail": message}, status_code=code)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(pg.PostgresError, handle_database_error)
    app.add_exception_handler(pg.InterfaceError, handle_database_error)
