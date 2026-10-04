from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.support import TOKEN, FakeClock, make_directory, new_redis
from tg_site_login.config import Settings
from tg_site_login.directory import InMemoryPhoneDirectory
from tg_site_login.login import LoginService
from tg_site_login.ratelimit import RateLimiter
from tg_site_login.redis_store import RedisStore
from tg_site_login.sessions import StoreSessionIssuer
from tg_site_login.store import MemoryStore, Store


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> MemoryStore:
    return MemoryStore(clock=clock)


@pytest.fixture
async def redis_store() -> AsyncIterator[RedisStore]:
    client = new_redis()
    yield RedisStore(client)
    await client.aclose()


@pytest.fixture(params=["memory", "redis"])
async def any_store(request: pytest.FixtureRequest, clock: FakeClock) -> AsyncIterator[Store]:
    """Run a test against both ``Store`` implementations."""
    if request.param == "memory":
        yield MemoryStore(clock=clock)
        return
    client = new_redis()
    yield RedisStore(client)
    await client.aclose()


@pytest.fixture
def directory() -> InMemoryPhoneDirectory:
    return make_directory()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        bot_token=TOKEN,
        bot_username="example_shop_bot",
        site_name="shop.example",
        store_backend="memory",
        allowed_origins=["https://shop.example"],
    )


def make_login(
    store: Store,
    directory: InMemoryPhoneDirectory,
    clock: FakeClock,
    *,
    enabled: bool = True,
    bad_code_limit: int = 5,
) -> LoginService:
    return LoginService(
        store,
        directory,
        StoreSessionIssuer(store),
        ttl=300,
        clock=clock,
        bad_code_limiter=RateLimiter(store, "badcode", limit=bad_code_limit, window=600),
        enabled=enabled,
    )


@pytest.fixture
def login(store: MemoryStore, directory: InMemoryPhoneDirectory, clock: FakeClock) -> LoginService:
    return make_login(store, directory, clock)
