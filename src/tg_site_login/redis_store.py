"""Redis implementation of ``Store`` on top of redis-py (asyncio)."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


def _millis(ttl: float) -> int:
    return max(1, math.ceil(ttl * 1000))


def _text(value: str | bytes | None) -> str | None:
    if isinstance(value, bytes):
        return value.decode()
    return value


class RedisStore:
    """``Store`` backed by Redis 7+ (``EXPIRE ... NX`` and ``GETDEL`` are required).

    The client must be created with ``decode_responses=True``.
    """

    def __init__(self, client: Redis, *, prefix: str = "tgsl:") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    @classmethod
    def from_url(cls, url: str, *, prefix: str = "tgsl:", timeout: float = 5.0) -> RedisStore:
        from redis.asyncio import Redis as AsyncRedis  # noqa: PLC0415

        client: Redis = AsyncRedis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
            health_check_interval=30,
        )
        return cls(client, prefix=prefix)

    async def get(self, key: str) -> str | None:
        return _text(await self._client.get(self._key(key)))

    async def set(self, key: str, value: str, ttl: float, *, nx: bool = False) -> bool:
        result = await self._client.set(self._key(key), value, px=_millis(ttl), nx=nx)
        return result is not None and bool(result)

    async def get_and_delete(self, key: str) -> str | None:
        return _text(await self._client.getdel(self._key(key)))

    async def delete(self, key: str) -> None:
        await self._client.delete(self._key(key))

    async def incr(self, key: str, ttl: float) -> int:
        full_key = self._key(key)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(full_key)
            pipe.pexpire(full_key, _millis(ttl), nx=True)
            results = await pipe.execute()
        return int(results[0])

    async def aclose(self) -> None:
        await self._client.aclose()
