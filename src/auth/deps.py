from typing import Annotated
from uuid import UUID

from asyncpg import Record
from fastapi import Cookie, Depends, HTTPException, status

from ..config import Config
from ..database.db import DBSession
from ..cache.cache import CacheSession
from ..database.models import User
from .auth import check_token

TokenCookie = Annotated[str | None, Cookie(alias=Config.auth_cookie_name)]


async def get_user(
    db: DBSession, cache: CacheSession, token: TokenCookie = None
) -> User | None:
    user_id = check_token(token)
    if not user_id:
        return None
    try:
        user_id = UUID(user_id)
    except ValueError:
        return None
    cache_user = await cache.get(f"user:{user_id}", User)
    if cache_user:
        return cache_user
    row: Record | None = await db.fetchrow("select * from users where uid=$1", user_id)
    if row is None:
        return None
    user = User(**row)
    await cache.set(f"user:{user_id}", user)
    return user


async def require_user(user: Annotated[User | None, Depends(get_user)]) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    return user


CurrentUser = Annotated[User, Depends(require_user)]
OptionalUser = Annotated[User | None, Depends(get_user)]
