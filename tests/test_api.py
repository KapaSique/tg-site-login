"""HTTP endpoints exercised through httpx's ASGI transport (no sockets, no Telegram)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe
from fastapi import FastAPI

from tests.conftest import make_login
from tests.support import CHAT_ID, FakeClock, FakeSession, RecordingSleep, make_bot
from tg_site_login.api import create_app
from tg_site_login.config import Settings
from tg_site_login.directory import InMemoryPhoneDirectory
from tg_site_login.login import LoginService
from tg_site_login.notifier import Notifier
from tg_site_login.retry import RetryPolicy
from tg_site_login.store import MemoryStore

ORIGIN = {"Origin": "https://shop.example"}
ANNA = 111
NONCE_COOKIE = "__Host-tgsl_nonce"


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "bot_token": "123456789:TEST-TOKEN-NOT-REAL",
        "bot_username": "example_shop_bot",
        "store_backend": "memory",
        "allowed_origins": ["https://shop.example"],
    }
    return Settings(**(base | overrides))


@asynccontextmanager
async def browser(
    app: FastAPI, base_url: str = "https://shop.example"
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        yield client


@pytest.fixture
def app(store: MemoryStore, login: LoginService, settings: Settings) -> FastAPI:
    return create_app(settings=settings, login=login, store=store)


async def bot_confirms(login: LoginService, deep_link: str, phone: str = "89991234567") -> None:
    """Play the bot's part: open the deep link, then share the user's own contact."""
    code = deep_link.split("?start=")[1]
    await login.claim(code, ANNA)
    await login.submit_contact(ANNA, ANNA, phone)


# -- start ----------------------------------------------------------------------------------


async def test_start_returns_deep_link_and_sets_a_hardened_cookie(
    app: FastAPI, clock: FakeClock
) -> None:
    async with browser(app) as client:
        response = await client.post("/auth/telegram/start", headers=ORIGIN)

    assert response.status_code == 200
    body = response.json()
    assert re.fullmatch(
        r"https://t\.me/example_shop_bot\?start=[A-Za-z0-9_-]{32}", body["deep_link"]
    )
    assert body["expires_at"].endswith("Z")
    assert re.fullmatch(r"\d{4}", body["pairing_code"])
    assert set(body) == {"deep_link", "expires_at", "pairing_code"}
    assert response.headers["cache-control"] == "no-store"

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{NONCE_COOKIE}=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=300"):
        assert attribute in cookie
    assert "Domain" not in cookie  # required for the __Host- prefix
    # the nonce is not part of the response body
    nonce = re.match(rf"{NONCE_COOKIE}=([^;]+)", cookie)
    assert nonce is not None
    assert nonce.group(1) not in response.text
    assert nonce.group(1) not in body["deep_link"]


async def test_cookie_is_not_secure_only_when_configured_for_local_dev(
    store: MemoryStore, login: LoginService
) -> None:
    app = create_app(
        settings=make_settings(cookie_secure=False, allowed_origins=["http://localhost:8000"]),
        login=login,
        store=store,
    )
    async with browser(app, "http://localhost:8000") as client:
        response = await client.post(
            "/auth/telegram/start", headers={"Origin": "http://localhost:8000"}
        )
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("tgsl_nonce=")  # __Host- needs Secure, so the name changes
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"Origin": "https://shop.example"}, True),
        ({"Origin": "https://evil.example"}, False),
        ({"Origin": "null"}, False),
        ({"Origin": "https://shop.example.evil.example"}, False),
        ({"Origin": "http://shop.example"}, True),  # same host, proxy terminated TLS
        ({}, True),  # no Origin and no fetch metadata: a non-browser client
        ({"Sec-Fetch-Site": "same-origin"}, True),
        ({"Sec-Fetch-Site": "cross-site"}, False),
        ({"Sec-Fetch-Site": "same-site"}, False),  # a sibling subdomain is not the site itself
    ],
)
async def test_start_is_protected_against_cross_site_requests(
    app: FastAPI, headers: dict[str, str], allowed: bool
) -> None:
    async with browser(app) as client:
        response = await client.post("/auth/telegram/start", headers=headers)
    assert response.status_code == (200 if allowed else 403)
    if not allowed:
        assert "set-cookie" not in response.headers
        assert response.json() == {"detail": "origin_not_allowed"}


async def test_additional_origin_can_be_allowed(store: MemoryStore, login: LoginService) -> None:
    app = create_app(
        settings=make_settings(allowed_origins="https://shop.example, https://app.example/"),
        login=login,
        store=store,
    )
    async with browser(app) as client:
        ok = await client.post("/auth/telegram/start", headers={"Origin": "https://app.example"})
    assert ok.status_code == 200


async def test_start_is_rate_limited_per_ip(store: MemoryStore, login: LoginService) -> None:
    app = create_app(
        settings=make_settings(start_rate_limit=3, start_rate_window_seconds=60),
        login=login,
        store=store,
    )
    async with browser(app) as client:
        statuses = [
            (await client.post("/auth/telegram/start", headers=ORIGIN)).status_code
            for _ in range(5)
        ]
        blocked = await client.post("/auth/telegram/start", headers=ORIGIN)
    assert statuses == [200, 200, 200, 429, 429]
    assert blocked.headers["retry-after"] == "60"
    assert "set-cookie" not in blocked.headers


async def test_spoofed_forwarded_for_does_not_dodge_the_limit(
    store: MemoryStore, login: LoginService
) -> None:
    app = create_app(settings=make_settings(start_rate_limit=2), login=login, store=store)
    async with browser(app) as client:
        statuses = [
            (
                await client.post(
                    "/auth/telegram/start", headers={**ORIGIN, "X-Forwarded-For": f"10.0.0.{n}"}
                )
            ).status_code
            for n in range(4)
        ]
    assert statuses == [200, 200, 429, 429]


async def test_configured_proxy_header_selects_the_limiter_bucket(
    store: MemoryStore, login: LoginService
) -> None:
    app = create_app(
        settings=make_settings(start_rate_limit=1, client_ip_header="X-Real-IP"),
        login=login,
        store=store,
    )

    async def start(ip: str) -> int:
        async with browser(app) as client:
            response = await client.post(
                "/auth/telegram/start", headers={**ORIGIN, "X-Real-IP": ip}
            )
        return response.status_code

    assert await start("203.0.113.1") == 200
    assert await start("203.0.113.1") == 429
    assert await start("203.0.113.2") == 200  # a different client has its own bucket


# -- poll -----------------------------------------------------------------------------------


async def test_poll_without_a_cookie_is_expired(app: FastAPI) -> None:
    async with browser(app) as client:
        response = await client.get("/auth/telegram/poll")
    assert response.status_code == 200
    assert response.json() == {"status": "expired"}
    assert response.headers["cache-control"] == "no-store"


async def test_full_login_flow(app: FastAPI, login: LoginService) -> None:
    async with browser(app) as client:
        start = (await client.post("/auth/telegram/start", headers=ORIGIN)).json()

        pending = await client.get("/auth/telegram/poll")
        assert pending.json() == {"status": "pending"}
        assert "set-cookie" not in pending.headers  # still waiting, cookie stays

        await bot_confirms(login, start["deep_link"])

        confirmed = await client.get("/auth/telegram/poll")
        assert confirmed.json() == {"status": "confirmed"}
        cookies = confirmed.headers.get_list("set-cookie")
        session = next(c for c in cookies if c.startswith("session="))
        for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=2592000"):
            assert attribute in session
        nonce = next(c for c in cookies if c.startswith(NONCE_COOKIE))
        assert "Max-Age=0" in nonce  # the nonce cookie is cleared once the attempt is over
        assert client.cookies.get("session")

        again = await client.get("/auth/telegram/poll")  # one-time: the code is burned
        assert again.json() == {"status": "expired"}


async def test_poll_from_another_browser_never_gets_the_session(
    app: FastAPI, login: LoginService
) -> None:
    async with browser(app) as victim, browser(app) as attacker:
        start = (await victim.post("/auth/telegram/start", headers=ORIGIN)).json()
        await bot_confirms(login, start["deep_link"])

        # the attacker knows the code from the deep link but not the nonce cookie
        code = start["deep_link"].split("?start=")[1]
        for forged in (code, "a" * 43, ""):
            attacker.cookies.set(NONCE_COOKIE, forged, domain="shop.example")
            stolen = await attacker.get("/auth/telegram/poll")
            assert stolen.json() == {"status": "expired"}
            assert "session=" not in "".join(stolen.headers.get_list("set-cookie"))

        # the code was not burned by the failed attempts
        legit = await victim.get("/auth/telegram/poll")
        assert legit.json() == {"status": "confirmed"}


@pytest.mark.parametrize(
    ("phone", "reason"),
    [("+7 999 555 00 00", "not_found"), ("8 999 000 00 02", "review_required")],
)
async def test_denied_login_reports_the_reason(
    app: FastAPI, login: LoginService, phone: str, reason: str
) -> None:
    async with browser(app) as client:
        start = (await client.post("/auth/telegram/start", headers=ORIGIN)).json()
        await bot_confirms(login, start["deep_link"], phone)
        response = await client.get("/auth/telegram/poll")
    assert response.json() == {"status": "denied", "reason": reason}
    assert "session=" not in "".join(response.headers.get_list("set-cookie"))


async def test_expired_attempt(app: FastAPI, clock: FakeClock) -> None:
    async with browser(app) as client:
        await client.post("/auth/telegram/start", headers=ORIGIN)
        clock.advance(301)
        assert (await client.get("/auth/telegram/poll")).json() == {"status": "expired"}


async def test_restarting_replaces_the_previous_attempt(app: FastAPI, login: LoginService) -> None:
    async with browser(app) as client:
        first = (await client.post("/auth/telegram/start", headers=ORIGIN)).json()
        second = (await client.post("/auth/telegram/start", headers=ORIGIN)).json()
        assert first["deep_link"] != second["deep_link"]
        # the first deep link no longer works
        old_code = first["deep_link"].split("?start=")[1]
        assert (await login.claim(old_code, ANNA)).status.value == "invalid"


async def test_poll_is_rate_limited(store: MemoryStore, login: LoginService) -> None:
    app = create_app(settings=make_settings(poll_rate_limit=2), login=login, store=store)
    async with browser(app) as client:
        statuses = [(await client.get("/auth/telegram/poll")).status_code for _ in range(4)]
        blocked = await client.get("/auth/telegram/poll")
    assert statuses == [200, 200, 429, 429]
    assert blocked.headers["retry-after"] == "60"


async def test_kill_switch_returns_503(
    store: MemoryStore, directory: InMemoryPhoneDirectory, clock: FakeClock
) -> None:
    login = make_login(store, directory, clock, enabled=False)
    app = create_app(settings=make_settings(), login=login, store=store)
    async with browser(app) as client:
        start = await client.post("/auth/telegram/start", headers=ORIGIN)
        poll = await client.get("/auth/telegram/poll")
    assert start.status_code == 503
    assert start.json() == {"detail": "login_disabled"}
    assert poll.status_code == 503


async def test_healthz(app: FastAPI) -> None:
    async with browser(app) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_shutdown_hooks_run(store: MemoryStore, login: LoginService) -> None:
    closed: list[str] = []

    async def hook() -> None:
        closed.append("closed")

    app = create_app(settings=make_settings(), login=login, store=store, on_shutdown=[hook])
    async with app.router.lifespan_context(app):
        assert closed == []
    assert closed == ["closed"]


# -- internal notify endpoint ---------------------------------------------------------------

AUTH = {"Authorization": "Bearer test-token-0123456789"}


def notify_app(
    store: MemoryStore, login: LoginService, session: FakeSession | None = None
) -> tuple[FastAPI, FakeSession]:
    bot, fake = make_bot(session)
    notifier = Notifier(
        bot,
        CHAT_ID,
        store,
        policy=RetryPolicy(max_attempts=2, base_delay=0.1),
        sleep=RecordingSleep(),
        min_interval=0,
    )
    app = create_app(
        settings=make_settings(notify_api_token="test-token-0123456789"),
        login=login,
        store=store,
        notifier=notifier,
    )
    return app, fake


def lead_payload(lead_id: str = "L-1", **overrides: Any) -> dict[str, Any]:
    return {"kind": "lead_created", "lead_id": lead_id, "name": "Boris <b>", **overrides}


async def test_notify_endpoint_is_absent_without_a_token(app: FastAPI) -> None:
    async with browser(app) as client:
        response = await client.post("/internal/notify", json=lead_payload())
    assert response.status_code == 404


async def test_notify_requires_the_bearer_token(store: MemoryStore, login: LoginService) -> None:
    app, session = notify_app(store, login)
    async with browser(app) as client:
        bad_headers: tuple[dict[str, str], ...] = (
            {},
            {"Authorization": "Bearer nope"},
            {"Authorization": "Bearer "},
            {"Authorization": "test-token-0123456789"},  # no scheme
            {"Authorization": "Basic test-token-0123456789"},
        )
        for headers in bad_headers:
            response = await client.post("/internal/notify", json=lead_payload(), headers=headers)
            assert response.status_code == 401
    assert session.requests == []


async def test_notify_sends_dedupes_and_escapes(store: MemoryStore, login: LoginService) -> None:
    app, session = notify_app(store, login)
    order = {
        "kind": "order_created",
        "order_id": "1001",
        "customer_name": "Anna",
        "items": [{"title": "T-shirt <new>", "quantity": 2, "unit_price": "990"}],
        "phone": "+79991234567",
    }
    unpaid = {
        "kind": "order_unpaid",
        "order_id": "1001",
        "customer_name": "Anna",
        "total": "1980",
        "waiting_minutes": 30,
    }
    async with browser(app) as client:
        results = [
            (await client.post("/internal/notify", json=payload, headers=AUTH)).json()["result"]
            for payload in (order, order, unpaid, lead_payload())
        ]
    assert results == ["sent", "duplicate", "sent", "sent"]
    texts = [m.text or "" for m in session.delivered]
    assert "T-shirt &lt;new&gt;" in texts[0]
    assert "Итого: <b>1 980 ₽</b>" in texts[0]
    assert "Заказ не оплачен №1001" in texts[1]
    assert "Boris &lt;b&gt;" in texts[2]


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "unknown"},
        {"kind": "lead_created", "name": "no id"},
        {
            "kind": "order_created",
            "order_id": "1",
            "customer_name": "A",
            "items": [{"title": "x", "quantity": 0, "unit_price": "1"}],
        },
        {
            "kind": "order_unpaid",
            "order_id": "1",
            "customer_name": "A",
            "total": "-5",
            "waiting_minutes": 1,
        },
    ],
)
async def test_notify_validates_the_payload(
    store: MemoryStore, login: LoginService, payload: dict[str, Any]
) -> None:
    app, session = notify_app(store, login)
    async with browser(app) as client:
        response = await client.post("/internal/notify", json=payload, headers=AUTH)
    assert response.status_code == 422
    assert session.requests == []


async def test_notify_reports_delivery_failure_as_502(
    store: MemoryStore, login: LoginService
) -> None:
    session = FakeSession()
    session.fail_next(
        *(TelegramNetworkError(method=GetMe(), message="down") for _ in range(2))
    )  # max_attempts
    app, _ = notify_app(store, login, session)
    async with browser(app) as client:
        response = await client.post("/internal/notify", json=lead_payload(), headers=AUTH)
        assert response.status_code == 502
        assert response.json() == {"detail": "delivery_failed"}
        # the failure did not poison the dedupe key: the caller's retry is delivered
        retry = await client.post("/internal/notify", json=lead_payload(), headers=AUTH)
    assert retry.json() == {"result": "sent"}


async def test_notify_kill_switch(store: MemoryStore, login: LoginService) -> None:
    bot, session = make_bot()
    notifier = Notifier(bot, CHAT_ID, store, is_enabled=lambda: False, min_interval=0)
    app = create_app(
        settings=make_settings(notify_api_token="test-token-0123456789"),
        login=login,
        store=store,
        notifier=notifier,
    )
    async with browser(app) as client:
        response = await client.post("/internal/notify", json=lead_payload(), headers=AUTH)
    assert response.json() == {"result": "disabled"}
    assert session.requests == []


def test_make_settings_is_hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_USERNAME", "from_env_bot")
    assert make_settings().bot_username == "example_shop_bot"  # explicit kwargs win


_unused: Callable[..., object] | None = None
