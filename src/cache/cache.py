from fastapi import Request, Depends
import json
import logging
from pydantic import BaseModel
from contextlib import asynccontextmanager
from typing import Annotated, Any, TypeVar
import redis.asyncio as redis
from redis.exceptions import RedisError
from ..config import Config

logger = logging.getLogger(__name__)


@asynccontextmanager
async def redis_errors():
    try:
        yield
    except RedisError as e:
        logger.warning("redis error: %s", e)


T = TypeVar("T", bound=BaseModel)


class Cache:
    def __init__(self, client: redis.Redis):
        self._redis = client

    async def set(self, key, value, ttl=Config.ttl_seconds) -> bool:
        data = (
            value.model_dump_json()
            if isinstance(value, BaseModel)
            else json.dumps(value)
        )
        async with redis_errors():
            await self._redis.set(key, data, ex=ttl)
            return True
        return False

    async def get(self, key, model: type[T] | None = None) -> T | Any | None:
        async with redis_errors():
            value = await self._redis.get(key)
            if value is None:
                return None
            if model:
                return model.model_validate_json(value)
            return json.loads(value)
        return None

    async def purge(self, *keys) -> bool:
        if not keys:
            return True
        async with redis_errors():
            await self._redis.delete(*keys)
            return True
        return False


def create_cache_client() -> redis.Redis:
    pool = redis.ConnectionPool.from_url(
        url=Config.cache_uri,
        max_connections=20,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    return redis.Redis(connection_pool=pool, auto_close_connection_pool=True)


def get_cache(request: Request) -> Cache:
    return Cache(request.state.cache)


CacheSession = Annotated[Cache, Depends(get_cache)]
