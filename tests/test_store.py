"""Contract tests: the same behaviour for ``MemoryStore`` and ``RedisStore`` (fakeredis)."""

from __future__ import annotations

import asyncio

import pytest

from tests.support import FakeClock, new_redis
from tg_site_login.ratelimit import RateLimiter
from tg_site_login.redis_store import RedisStore
from tg_site_login.store import MemoryStore, Store


async def test_set_and_get(any_store: Store) -> None:
    assert await any_store.get("k") is None
    assert await any_store.set("k", "v", 60) is True
    assert await any_store.get("k") == "v"


async def test_set_overwrites_without_nx(any_store: Store) -> None:
    await any_store.set("k", "one", 60)
    assert await any_store.set("k", "two", 60) is True
    assert await any_store.get("k") == "two"


async def test_set_nx_only_writes_once(any_store: Store) -> None:
    assert await any_store.set("k", "first", 60, nx=True) is True
    assert await any_store.set("k", "second", 60, nx=True) is False
    assert await any_store.get("k") == "first"


async def test_get_and_delete_is_one_shot(any_store: Store) -> None:
    await any_store.set("k", "v", 60)
    assert await any_store.get_and_delete("k") == "v"
    assert await any_store.get_and_delete("k") is None
    assert await any_store.get("k") is None


async def test_concurrent_get_and_delete_has_one_winner(any_store: Store) -> None:
    await any_store.set("k", "v", 60)
    results = await asyncio.gather(*(any_store.get_and_delete("k") for _ in range(10)))
    assert results.count("v") == 1
    assert results.count(None) == 9


async def test_delete_ignores_missing_keys(any_store: Store) -> None:
    await any_store.delete("missing")
    await any_store.set("k", "v", 60)
    await any_store.delete("k")
    assert await any_store.get("k") is None


async def test_incr_counts_and_keeps_one_window(any_store: Store) -> None:
    assert [await any_store.incr("c", 60) for _ in range(3)] == [1, 2, 3]


async def test_concurrent_incr_never_loses_updates(any_store: Store) -> None:
    results = await asyncio.gather(*(any_store.incr("c", 60) for _ in range(20)))
    assert sorted(results) == list(range(1, 21))


async def test_memory_store_expiry(clock: FakeClock) -> None:
    store = MemoryStore(clock=clock)
    await store.set("k", "v", 10)
    clock.advance(9.9)
    assert await store.get("k") == "v"
    clock.advance(0.2)
    assert await store.get("k") is None
    # an expired key can be taken again with nx
    assert await store.set("k", "again", 10, nx=True) is True


async def test_memory_store_incr_window_resets_after_ttl(clock: FakeClock) -> None:
    store = MemoryStore(clock=clock)
    assert await store.incr("c", 10) == 1
    clock.advance(5)
    assert await store.incr("c", 10) == 2  # ttl is not extended by later increments
    clock.advance(5.1)
    assert await store.incr("c", 10) == 1


async def test_redis_store_expiry_and_prefix() -> None:
    client = new_redis()
    store = RedisStore(client, prefix="p:")
    await store.set("k", "v", 0.05)
    assert await client.get("p:k") == "v"
    await asyncio.sleep(0.12)
    assert await store.get("k") is None
    await client.aclose()


async def test_redis_store_incr_sets_ttl_once() -> None:
    client = new_redis()
    store = RedisStore(client)
    await store.incr("c", 60)
    first_ttl = await client.pttl("tgsl:c")
    await asyncio.sleep(0.05)
    await store.incr("c", 60)
    second_ttl = await client.pttl("tgsl:c")
    assert 0 < second_ttl <= first_ttl  # the second increment did not reset the window
    await client.aclose()


async def test_rate_limiter_blocks_after_limit(any_store: Store) -> None:
    limiter = RateLimiter(any_store, "t", limit=3, window=60)
    verdicts = [await limiter.hit("1.2.3.4") for _ in range(5)]
    assert [v.allowed for v in verdicts] == [True, True, True, False, False]
    assert verdicts[-1].retry_after == 60
    assert (await limiter.hit("5.6.7.8")).allowed  # other subjects have their own bucket


async def test_rate_limiter_window_resets(clock: FakeClock) -> None:
    limiter = RateLimiter(MemoryStore(clock=clock), "t", limit=1, window=10)
    assert (await limiter.hit("x")).allowed
    assert not (await limiter.hit("x")).allowed
    clock.advance(10.1)
    assert (await limiter.hit("x")).allowed


def test_rate_limiter_rejects_nonsense_config(store: MemoryStore) -> None:
    with pytest.raises(ValueError, match="limit"):
        RateLimiter(store, "t", limit=0, window=10)
