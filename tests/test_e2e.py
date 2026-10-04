"""API process and bot process in one test, talking only through a shared Redis store."""

from __future__ import annotations

from collections.abc import AsyncIterator

import fakeredis
import httpx
import pytest
from aiogram import Dispatcher
from aiogram.types import ReplyKeyboardMarkup

from tests.support import contact_of, make_bot, make_directory, new_redis, private_message
from tg_site_login.api import create_app
from tg_site_login.bot.handlers import build_router
from tg_site_login.config import Settings
from tg_site_login.directory import InMemoryPhoneDirectory
from tg_site_login.login import LoginService
from tg_site_login.redis_store import RedisStore
from tg_site_login.sessions import StoreSessionIssuer

ORIGIN = {"Origin": "https://shop.example"}
ANNA = 111


@pytest.fixture
async def redis_client() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    client = new_redis()
    yield client
    await client.aclose()


def make_service(
    client: fakeredis.FakeAsyncRedis, directory: InMemoryPhoneDirectory
) -> LoginService:
    """Each process builds its own service object; only Redis is shared."""
    store = RedisStore(client)
    return LoginService(store, directory, StoreSessionIssuer(store), ttl=300)


async def test_browser_api_and_bot_complete_a_login(
    redis_client: fakeredis.FakeAsyncRedis, settings: Settings
) -> None:
    api_login = make_service(redis_client, make_directory())
    app = create_app(settings=settings, login=api_login, store=RedisStore(redis_client))

    bot, session = make_bot()
    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_router(make_service(redis_client, make_directory()), site_name="shop.example")
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://shop.example") as browser:
        # 1. the browser asks for a login link
        start = (await browser.post("/auth/telegram/start", headers=ORIGIN)).json()
        assert (await browser.get("/auth/telegram/poll")).json() == {"status": "pending"}

        # 2. the user opens the deep link: Telegram sends "/start <code>" to the bot
        code = start["deep_link"].split("?start=")[1]
        await dispatcher.feed_update(bot, private_message(ANNA, text=f"/start {code}"))
        (ask,) = session.delivered
        assert isinstance(ask.reply_markup, ReplyKeyboardMarkup)
        assert start["pairing_code"] in (ask.text or "")  # same code on screen and in the chat
        assert (await browser.get("/auth/telegram/poll")).json() == {"status": "pending"}

        # 3. the user presses the share-contact button
        contact = private_message(ANNA, contact=contact_of(ANNA, "79991234567"), update_id=2)
        await dispatcher.feed_update(bot, contact)
        assert "Готово, Anna" in (session.delivered[-1].text or "")

        # 4. the browser's next poll receives the session, exactly once
        done = await browser.get("/auth/telegram/poll")
        assert done.json() == {"status": "confirmed"}
        assert browser.cookies.get("session")
        assert (await browser.get("/auth/telegram/poll")).json() == {"status": "expired"}

    session_token = browser.cookies.get("session")
    assert session_token is not None
    issuer = StoreSessionIssuer(RedisStore(redis_client))
    customer = await issuer.resolve(session_token)
    assert customer is not None
    assert customer.customer_id == "c-1"


async def test_attacker_who_phishes_the_link_still_cannot_read_the_session_of_another_browser(
    redis_client: fakeredis.FakeAsyncRedis, settings: Settings
) -> None:
    """The victim confirms the attacker's link; a third browser still gets nothing."""
    app = create_app(
        settings=settings,
        login=make_service(redis_client, make_directory()),
        store=RedisStore(redis_client),
    )
    bot, _ = make_bot()
    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_router(make_service(redis_client, make_directory()), site_name="shop.example")
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://shop.example") as attacker,
        httpx.AsyncClient(transport=transport, base_url="https://shop.example") as bystander,
    ):
        link = (await attacker.post("/auth/telegram/start", headers=ORIGIN)).json()["deep_link"]
        code = link.split("?start=")[1]
        await dispatcher.feed_update(bot, private_message(ANNA, text=f"/start {code}"))
        await dispatcher.feed_update(
            bot, private_message(ANNA, contact=contact_of(ANNA, "79991234567"), update_id=2)
        )

        assert (await bystander.get("/auth/telegram/poll")).json() == {"status": "expired"}
        assert not bystander.cookies.get("session")
        # (the attacker's own browser does receive the session: that is the phishing risk the
        # pairing code is for, see the README security model)
        assert (await attacker.get("/auth/telegram/poll")).json() == {"status": "confirmed"}
