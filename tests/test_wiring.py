from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tg_site_login import wiring
from tg_site_login.config import Settings
from tg_site_login.directory import Customer, InMemoryPhoneDirectory, PhoneDirectory
from tg_site_login.redis_store import RedisStore
from tg_site_login.sessions import SessionIssuer, StoreSessionIssuer
from tg_site_login.store import MemoryStore, Store


def custom_directory(settings: Settings) -> PhoneDirectory:
    directory = InMemoryPhoneDirectory()
    directory.add("+79990001111", Customer("factory", settings.site_name))
    return directory


def custom_issuer(settings: Settings, store: Store) -> SessionIssuer:
    return StoreSessionIssuer(store, cookie_name="custom")


def test_load_object() -> None:
    assert wiring.load_object("tests.test_wiring:custom_directory") is custom_directory
    for bad in ("nocolon", ":attr", "module:"):
        with pytest.raises(ValueError, match=r"package\.module:attribute"):
            wiring.load_object(bad)
    with pytest.raises(ModuleNotFoundError):
        wiring.load_object("no_such_package:thing")


def test_store_backend_selection(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(wiring.build_store(settings), MemoryStore)

    redis_settings = settings.model_copy(update={"store_backend": "redis"})
    store = wiring.build_store(redis_settings)
    assert isinstance(store, RedisStore)  # lazy: no connection is opened yet


async def test_directory_factories(settings: Settings, tmp_path: Path) -> None:
    assert isinstance(wiring.build_directory(settings), InMemoryPhoneDirectory)

    path = tmp_path / "customers.json"
    path.write_text(json.dumps([{"phone": "+79990002222", "customer_id": "f", "name": "File"}]))
    from_file = wiring.build_directory(settings.model_copy(update={"directory_file": str(path)}))
    assert (await from_file.lookup("+79990002222")).customer == Customer("f", "File")

    custom = wiring.build_directory(
        settings.model_copy(update={"directory_factory": "tests.test_wiring:custom_directory"})
    )
    assert (await custom.lookup("+79990001111")).customer == Customer("factory", "shop.example")


async def test_issuer_factory_and_defaults(settings: Settings) -> None:
    store = MemoryStore()
    default = wiring.build_issuer(settings, store)
    assert (await default.issue(Customer("c"))).cookie_name == "session"

    custom = wiring.build_issuer(
        settings.model_copy(update={"session_issuer_factory": "tests.test_wiring:custom_issuer"}),
        store,
    )
    assert (await custom.issue(Customer("c"))).cookie_name == "custom"


async def test_login_service_uses_settings(settings: Settings) -> None:
    store = MemoryStore()
    service = wiring.build_login_service(
        settings.model_copy(update={"login_enabled": False}), store
    )
    assert service.enabled is False
    enabled = wiring.build_login_service(settings, store)
    started = await enabled.start()
    assert started.expires_at - __import__("time").time() == pytest.approx(300, abs=5)


def test_retry_policy_comes_from_settings(settings: Settings) -> None:
    policy = wiring.build_retry_policy(
        settings.model_copy(update={"notify_max_attempts": 7, "telegram_request_timeout": 9.0})
    )
    assert (policy.max_attempts, policy.request_timeout) == (7, 9.0)


def test_logging_hides_http_clients_that_print_urls() -> None:
    wiring.configure_logging("info")
    for name in ("httpx", "httpcore", "aiohttp.client"):
        assert logging.getLogger(name).level == logging.WARNING


async def test_app_factory_builds_a_working_app(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setenv("BOT_TOKEN", "123456789:TEST-TOKEN-NOT-REAL")
    monkeypatch.setenv("BOT_USERNAME", "example_shop_bot")
    monkeypatch.setenv("STORE_BACKEND", "memory")
    monkeypatch.setenv("NOTIFY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("NOTIFY_API_TOKEN", "0123456789abcdef")
    monkeypatch.chdir(Path(__file__).parent)  # no .env here
    app = wiring.create_app_from_env()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://shop.example") as http:
            assert (await http.get("/healthz")).status_code == 200
            start = await http.post(
                "/auth/telegram/start", headers={"Origin": "https://shop.example"}
            )
            assert start.status_code == 200
            # notifications are wired because chat id and token are present
            unauthorized = await http.post("/internal/notify", json={})
            assert unauthorized.status_code == 401
