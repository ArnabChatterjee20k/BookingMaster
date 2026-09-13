from fastapi import Request, Depends
from typing import Annotated
import redis.asyncio as redis
from ..config import Config


def create_cache_client() -> redis.Redis:
    pool = redis.ConnectionPool.from_url(
        url=Config.cache_uri,
        max_connections=20,
        decode_responses=True,
    )
    return redis.Redis(connection_pool=pool, auto_close_connection_pool=True)


def get_cache(request: Request) -> redis.Redis:
    return request.state.cache


CacheSession = Annotated[redis.Redis, Depends(get_cache)]
