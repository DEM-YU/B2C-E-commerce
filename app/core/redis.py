from __future__ import annotations

from typing import Any, Awaitable, Protocol
from redis.asyncio import Redis as _RedisClient


class AsyncRedis(Protocol):
    """
    Protocol definition for Async Redis client to resolve static analysis errors
    in Pylance/Pyright (e.g. "Type 'str' is not awaitable" caused by union return types in redis-py stubs).
    """
    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Awaitable[Any]: ...
    def hget(self, name: str, key: str) -> Awaitable[Any]: ...
    def hset(self, name: str, key: str = ..., value: Any = ..., mapping: Any = ...) -> Awaitable[Any]: ...
    def hdel(self, name: str, *keys: str) -> Awaitable[Any]: ...
    def expire(self, name: str, time: Any) -> Awaitable[Any]: ...
    def delete(self, *names: str) -> Awaitable[Any]: ...
    def hgetall(self, name: str) -> Awaitable[Any]: ...
    def pipeline(self, transaction: bool = True, shard_hint: Any = None) -> Any: ...


_redis_client: _RedisClient | None = None

REDIS_URL = "redis://localhost:6380/0"


def get_redis_client() -> _RedisClient:
    global _redis_client
    if _redis_client is None:
        _redis_client = _RedisClient.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_client


async def get_redis() -> AsyncRedis:
    return get_redis_client()  # type: ignore[return-value]
