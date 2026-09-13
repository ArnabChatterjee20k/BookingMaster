from fastapi import Request, Depends
import json
from pydantic import BaseModel
from contextlib import asynccontextmanager
from typing import Annotated, TypeVar
import redis.asyncio as redis
from redis.exceptions import ConnectionError, TimeoutError
from ..config import Config


@asynccontextmanager
async def redis_errors():
    try:
        yield
    except ConnectionError:
        print("Redis connection failed")
        return None
    except TimeoutError:
        print("Redis request timed out")
        return None
    except Exception as e:
        print(f"Redis error: {e}")
        return None


T = TypeVar("T", bound=BaseModel)


class Cache:
    def __init__(self, redis: redis.Redis):
        self._redis: redis.Redis = redis

    async def set(self, key, value, ttl=Config.ttl_seconds) -> True | None:
        async with redis_errors():
            # always encoding to json to reliably always converting to json decode
            self._redis.set(key, json.dumps(value), ttl)
            return True

    async def get(self, key, model: T) -> T | None:
        async with redis_errors():
            value = self._redis.get(key)
            if not value:
                return None
            value = json.loads(value)
            if model:
                return model.model_validate(value)
            return value


def create_cache_client() -> redis.Redis:
    pool = redis.ConnectionPool.from_url(
        url=Config.cache_uri,
        max_connections=20,
        decode_responses=True,
    )
    return redis.Redis(connection_pool=pool, auto_close_connection_pool=True)


def get_cache(request: Request) -> redis.Redis:
    return Cache(request.state.cache)


CacheSession = Annotated[Cache, Depends(get_cache)]
